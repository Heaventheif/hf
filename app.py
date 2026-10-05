from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from segmenter import Segmenter
from stt import SpeechToText
from translator import Translator
from tts import ArabicTTS

logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO)
log = logging.getLogger("dubbing")
executor = ThreadPoolExecutor(max_workers=3)
stt: SpeechToText | None = None
translator: Translator | None = None
tts: ArabicTTS | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global stt, translator, tts
    loop = asyncio.get_running_loop()
    stt, translator, tts = await asyncio.gather(
        loop.run_in_executor(executor, SpeechToText, settings.whisper_dir, settings.whisper_model, settings.whisper_device, settings.whisper_compute),
        loop.run_in_executor(executor, Translator, settings.translation_dir),
        loop.run_in_executor(executor, ArabicTTS, settings.piper_model),
    )
    log.info("models ready: whisper=%s piper_rate=%s", settings.whisper_model, tts.sample_rate)
    yield
    executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="YouTube Arabic AI Dubbing", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


@app.get("/health")
async def health():
    return {"status": "ok" if stt and translator and tts else "loading", "stt": stt is not None, "translation": translator is not None, "tts": tts is not None, "device": settings.whisper_device, "version": "1.0.0"}


@app.get("/voices")
async def voices():
    return [{"id": "ar_JO-kareem-medium", "name": "Kareem", "sample_rate": tts.sample_rate if tts else 22050}]


async def send_json(ws: WebSocket, payload: dict):
    await ws.send_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    session = 0
    hello = await ws.receive_text()
    try:
        msg = json.loads(hello)
    except json.JSONDecodeError:
        await send_json(ws, {"type": "error", "stage": "hello", "message": "invalid hello"})
        await ws.close(code=1003)
        return
    if msg.get("type") != "hello":
        await send_json(ws, {"type": "error", "stage": "hello", "message": "hello required"})
        await ws.close(code=1008)
        return
    session = int(msg.get("session", 1))
    src_lang = str(msg.get("src_lang", "auto"))
    media_t0 = float(msg.get("media_t0", 0.0))
    segmenter = Segmenter()
    seq = 0
    send_lock = asyncio.Lock()
    in_flight = asyncio.Semaphore(settings.max_in_flight)
    tasks: set[asyncio.Task] = set()

    async def send(payload: dict):
        async with send_lock:
            await send_json(ws, payload)

    async def process(seg, current_session: int, current_seq: int):
        async with in_flight:
            started = time.perf_counter()
            try:
                loop = asyncio.get_running_loop()
                text, detected, confidence = await loop.run_in_executor(executor, stt.transcribe, seg.pcm, None if src_lang == "auto" else src_lang)
                if not text or current_session != session:
                    return
                arabic = await loop.run_in_executor(executor, translator.translate, text, detected, "ar")
                if not arabic or current_session != session:
                    return
                source_duration = (seg.end_sample - seg.start_sample) / 16_000
                target_duration = max(0.8, min(1.15, 1.0 + (len(arabic) / max(1, len(text)) - 1.0) * 0.08))
                pcm = await loop.run_in_executor(executor, tts.synthesize, arabic, target_duration)
                if current_session != session:
                    return
                await send({"type": "segment", "session": current_session, "seq": current_seq, "src_start": media_t0 + seg.start_sample / 16_000, "src_end": media_t0 + seg.end_sample / 16_000, "text_src": text if settings.debug else "", "text_ar": arabic if settings.debug else "", "language": detected, "confidence": confidence, "sample_rate": tts.sample_rate})
                async with send_lock:
                    await ws.send_bytes(struct.pack(">II", current_session, current_seq) + pcm)
                log.info("segment=%s lang=%s %.2fs", current_seq, detected, time.perf_counter() - started)
            except Exception as exc:
                log.exception("segment failed")
                await send({"type": "error", "session": current_session, "seq": current_seq, "stage": "pipeline", "message": str(exc)[:200]})

    try:
        await send({"type": "ready", "session": session, "sample_rate": 16_000, "tts_sample_rate": tts.sample_rate})
        while True:
            incoming = await ws.receive()
            if incoming.get("type") == "websocket.disconnect":
                break
            if incoming.get("text") is not None:
                control = json.loads(incoming["text"])
                if control.get("type") == "ping":
                    await send({"type": "pong", "session": session})
                elif control.get("type") == "seek":
                    session = int(control.get("session", session + 1))
                    media_t0 = float(control.get("media_t0", 0.0))
                    segmenter = Segmenter()
                    seq = 0
                    await send({"type": "session", "session": session})
                elif control.get("type") == "flush":
                    for seg in segmenter.flush():
                        seq += 1
                        task = asyncio.create_task(process(seg, session, seq)); tasks.add(task); task.add_done_callback(tasks.discard)
                continue
            data = incoming.get("bytes")
            if not data or len(data) <= 8:
                continue
            packet_session, offset = struct.unpack(">II", data[:8])
            if packet_session != session:
                continue
            for seg in segmenter.push(data[8:], offset):
                seq += 1
                task = asyncio.create_task(process(seg, session, seq)); tasks.add(task); task.add_done_callback(tasks.discard)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        for task in tasks:
            task.cancel()
