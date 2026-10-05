"""YouTube Arabic AI Dubbing: FastAPI + WebSocket (Hugging Face Docker Space)."""
import asyncio
import hmac
import json
import logging
import re
import struct
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace

import numpy as np
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

import config as C
from segmenter import Segmenter
from translator import UnsupportedLanguage

logging.basicConfig(level=logging.DEBUG if C.DEBUG else logging.INFO,
                    format="%(asctime)s %(message)s")
log = logging.getLogger("ytdub")

E = SimpleNamespace(stt=None, mt=None, tts=None, device="cpu", ready=False, error=None)
POOL = ThreadPoolExecutor(max_workers=max(2, min(4, C.CPU_THREADS)))
FLUSH = object()
SENTENCE_END = re.compile(r"[.!?…。！？؟][\"')\]»”]*\s*$")


def load_engines():
    from stt import STT
    from translator import Translator
    from tts import TTS
    E.stt = STT()
    E.device = E.stt.device
    E.mt = Translator(E.device)
    E.tts = TTS()
    E.ready = True
    log.info("engines ready")


@asynccontextmanager
async def lifespan(app):
    loop = asyncio.get_running_loop()

    async def boot():
        try:
            await loop.run_in_executor(None, load_engines)
        except Exception as e:  # noqa
            E.error = str(e)
            log.exception("engine load failed")

    task = asyncio.create_task(boot())
    yield
    task.cancel()


app = FastAPI(title="YouTube Arabic Dubbing", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=C.CORS_ORIGINS, allow_methods=["GET"], allow_headers=["*"])


@app.get("/", response_class=HTMLResponse)
def index():
    return "<h3>YouTube Arabic AI Dubbing</h3><p>الخادم يعمل. استخدم الإضافة للاتصال بـ <code>/ws</code>.</p>"


@app.get("/health")
def health():
    status = "ok" if E.ready else ("error" if E.error else "loading")
    return {"status": status, "stt": E.stt is not None, "translation": E.mt is not None,
            "tts": E.tts is not None, "device": E.device, "version": C.VERSION,
            "stt_level": E.stt.level if E.stt else None, "error": E.error}


@app.get("/voices")
def voices():
    return E.tts.list_voices() if E.tts else []


def run(fn, *a):
    return asyncio.get_running_loop().run_in_executor(POOL, fn, *a)


class Session:
    def __init__(self, ws: WebSocket, hello: dict):
        self.ws = ws
        self.send_lock = asyncio.Lock()
        self.seg = Segmenter()
        self.task = None
        self.src_hint = hello.get("src_lang", "auto") or "auto"
        self.voice = self._pick_voice(hello.get("voice"))
        self.last_level = E.stt.level
        self.reset(hello)

    # ------------------------------------------------------------ حالة
    def _pick_voice(self, v):
        if v and E.tts.has(v):
            return v
        return C.DEFAULT_VOICE if E.tts.has(C.DEFAULT_VOICE) else E.tts.list_voices()[0]["id"]

    def reset(self, d: dict):
        if self.task:
            self.task.cancel()
        self.id = int(d.get("session", 0))
        self.t0 = float(d.get("media_t0", 0.0))
        if "voice" in d:
            self.voice = self._pick_voice(d["voice"])
        self.seg.reset(0)
        self.stream_end = 0
        self.q = deque()
        self.ev = asyncio.Event()
        self.cur_start = None
        self.pending = None            # (text, start, end)
        self.flush_req = False
        self.seq = 0
        self.emitted_until = self.t0
        self.lang = None
        self.n_utt = 0
        self.warned_lang = False
        self.task = asyncio.create_task(self.worker(self.id, self.q, self.ev))

    def tm(self, sample: int) -> float:
        return self.t0 + sample / C.SR

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    async def close(self):
        if self.task:
            self.task.cancel()

    # ------------------------------------------------------------ إرسال
    async def send_json(self, obj):
        try:
            async with self.send_lock:
                await self.ws.send_text(json.dumps(obj, ensure_ascii=False))
        except Exception:  # noqa
            pass

    async def send_segment(self, meta: dict, pcm: bytes):
        header = struct.pack("<III", meta["session"], meta["seq"], meta["sample_rate"])
        try:
            async with self.send_lock:
                await self.ws.send_text(json.dumps(meta, ensure_ascii=False))
                await self.ws.send_bytes(header + pcm)
        except Exception:  # noqa
            pass

    # ------------------------------------------------------------ وارد
    async def on_audio(self, data: bytes):
        if len(data) < 10:
            return
        sid, off = struct.unpack_from("<II", data, 0)
        if sid != self.id:
            return
        pcm = np.frombuffer(data, dtype="<i2", offset=8, count=(len(data) - 8) // 2)
        for u in self.seg.feed(pcm, off):
            if len(self.q) >= C.MAX_IN_FLIGHT:
                self.q.popleft()
                log.warning("طابور ممتلئ: إسقاط أقدم مقطع")
            self.q.append(u)
            self.ev.set()
        self.stream_end = off + len(pcm)
        # دمج الجمل: لا تنتظر مقطعاً تالياً إلى الأبد
        if (self.pending and not self.flush_req and not self.q and self.cur_start is None
                and self.tm(self.stream_end) - self.pending[2] > C.MERGE_GAP_SEC):
            self.flush_req = True
            self.q.append(FLUSH)
            self.ev.set()
        await self.maybe_silence()

    async def on_control(self, d: dict):
        t = d.get("type")
        if t == "ping":
            await self.send_json({"type": "pong"})
        elif t in ("seek", "hello"):
            self.reset(d)
        elif t == "resync":
            if int(d.get("session", -1)) == self.id:
                self.t0 = float(d["media_t"]) - int(d["sample_offset"]) / C.SR

    # ------------------------------------------------------------ تقدّم
    def frontier(self) -> float:
        c = [self.tm(self.seg.frontier_sample())]
        if self.cur_start is not None:
            c.append(self.cur_start)
        c += [self.tm(u.start) for u in self.q if u is not FLUSH]
        if self.pending:
            c.append(self.pending[1])
        return min(c)

    async def maybe_silence(self):
        f = self.frontier()
        if f - self.emitted_until >= 1.0:
            await self.send_json({"type": "silence", "session": self.id, "seq": self.next_seq(),
                                  "src_start": round(self.emitted_until, 3), "src_end": round(f, 3)})
            self.emitted_until = f

    # ------------------------------------------------------------ معالجة
    async def worker(self, sid, q, ev):
        while True:
            while not q:
                ev.clear()
                await ev.wait()
            item = q.popleft()
            if sid != self.id:
                return
            try:
                if item is FLUSH:
                    self.flush_req = False
                    if self.pending:
                        p, self.pending = self.pending, None
                        await self.emit(sid, *p)
                else:
                    await self.handle(sid, item)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa
                log.exception("pipeline error")
                await self.send_json({"type": "error", "session": sid, "seq": self.seq, "stage": "pipeline"})
            finally:
                self.cur_start = None
            if sid == self.id:
                await self.maybe_silence()

    def lang_for_stt(self):
        if self.src_hint != "auto":
            return self.src_hint
        self.n_utt += 1
        if self.lang is None or self.n_utt % 15 == 0:     # إعادة كشف دورية
            return None
        return self.lang

    async def handle(self, sid, u):
        t_all = time.perf_counter()
        self.cur_start = self.tm(u.start)
        text, lang, prob = await run(E.stt.transcribe, u.audio, self.lang_for_stt())
        t_stt = time.perf_counter() - t_all
        if sid != self.id:
            return
        log.info("[STT] seq=%d %.2fs (%s) dur=%.1fs", self.seq + 1, t_stt, E.stt.level, u.duration)
        if text and self.src_hint == "auto":
            if self.lang is None and prob >= 0.6:
                self.lang = lang
            elif self.lang is not None and lang != self.lang and prob >= 0.8:
                self.lang = lang
        if self.src_hint != "auto":
            self.lang = self.src_hint
        start, end = self.tm(u.start), self.tm(u.end)
        emits = []
        if text:
            p = self.pending
            if p and start - p[2] > C.MERGE_GAP_SEC:
                emits.append(p)
                p = None
            self.pending = None
            if p:
                text, start = p[0] + " " + text, p[1]
            if SENTENCE_END.search(text) or (end - start) >= C.MERGE_MAX_SEC:
                emits.append((text, start, end))
            else:
                self.pending = (text, start, end)
        t1 = time.perf_counter()
        for (tx, s, e) in emits:
            await self.emit(sid, tx, s, e)
        proc = t_stt + (time.perf_counter() - t1)
        rtf = proc / max(u.duration, 0.5)
        E.stt.report_rtf(rtf)
        if E.stt.level != self.last_level:
            self.last_level = E.stt.level
            await self.send_json({"type": "quality", "level": E.stt.level})

    async def emit(self, sid, text, start, end):
        lang = self.lang or "en"
        if lang == "ar":
            return                                       # المصدر عربي أصلاً
        seq = self.seq + 1
        t0 = time.perf_counter()
        try:
            text_ar = await run(E.mt.translate, text, lang, "ar")
        except UnsupportedLanguage:
            if not self.warned_lang:
                self.warned_lang = True
                await self.send_json({"type": "error", "session": sid, "seq": seq, "stage": "translate",
                                      "message": f"اللغة غير مدعومة: {lang}"})
            return
        t_mt = time.perf_counter() - t0
        if sid != self.id or not text_ar:
            return
        ls = E.tts.plan_length_scale(text_ar, self.voice, end - start)
        t1 = time.perf_counter()
        pcm, sr = await run(E.tts.synthesize, text_ar, self.voice, ls)
        t_tts = time.perf_counter() - t1
        if sid != self.id or not pcm:
            return
        log.info("[MT] seq=%d %.2fs [TTS] %.2fs ls=%.2f", seq, t_mt, t_tts, ls)
        meta = {"type": "segment", "session": sid, "seq": self.next_seq(),
                "src_start": round(start, 3), "src_end": round(end, 3), "sample_rate": sr,
                "length_scale": round(ls, 2), "duration": round(len(pcm) / 2 / sr, 3)}
        if C.DEBUG:
            meta["text_src"], meta["text_ar"] = text, text_ar
        await self.send_segment(meta, pcm)
        self.emitted_until = max(self.emitted_until, end)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if C.ALLOWED_ORIGINS and ws.headers.get("origin") not in C.ALLOWED_ORIGINS:
        await ws.close(code=4403)
        return
    await ws.accept()
    sess = None
    try:
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
        except Exception:  # noqa
            await ws.close(code=4400)
            return
        if hello.get("type") != "hello":
            await ws.close(code=4400)
            return
        if C.API_KEY and not hmac.compare_digest(str(hello.get("api_key", "")), C.API_KEY):
            await ws.send_text(json.dumps({"type": "error", "stage": "auth"}))
            await ws.close(code=4401)
            return
        if not E.ready:
            await ws.send_text(json.dumps({"type": "error", "stage": "startup",
                                           "message": "النماذج قيد التحميل"}))
            await ws.close(code=1013)
            return
        sess = Session(ws, hello)
        await sess.send_json({"type": "ready", "session": sess.id, "voice": sess.voice,
                              "stt_level": E.stt.level, "device": E.device})
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                await sess.on_audio(msg["bytes"])
            elif msg.get("text") is not None:
                d = json.loads(msg["text"])
                if d.get("type") == "bye":
                    break
                await sess.on_control(d)
    except Exception:  # noqa
        log.debug("ws closed", exc_info=True)
    finally:
        if sess:
            await sess.close()
