"""Phase 0 / الاختبار C: عميل WebSocket يرسل ملف WAV (16kHz mono PCM16) ويطبع الردود.
python tools/ws_test.py clip.wav wss://USER-SPACE.hf.space/ws [--key KEY] [--fast]
"""
import argparse
import asyncio
import json
import struct
import wave

from websockets.asyncio.client import connect

SID = 7


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("url")
    ap.add_argument("--key", default="")
    ap.add_argument("--voice", default="")
    ap.add_argument("--fast", action="store_true", help="إرسال أسرع من الزمن الحقيقي")
    ap.add_argument("--tail", type=float, default=10.0, help="ثوانٍ صمت بعد الملف")
    a = ap.parse_args()

    with wave.open(a.wav) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, \
            "المطلوب: 16kHz mono PCM16"
        pcm = w.readframes(w.getnframes())

    async with connect(a.url, max_size=None) as ws:
        hello = {"type": "hello", "session": SID, "src_lang": "auto", "api_key": a.key,
                 "media_t0": 0.0}
        if a.voice:
            hello["voice"] = a.voice
        await ws.send(json.dumps(hello))

        async def sender():
            step = 3200 * 2
            off = 0
            data = pcm + bytes(int(a.tail * 32000))
            for i in range(0, len(data), step):
                chunk = data[i:i + step]
                await ws.send(struct.pack("<II", SID, off) + chunk)
                off += len(chunk) // 2
                await asyncio.sleep(0 if a.fast else 0.2)
            await asyncio.sleep(15)
            await ws.close()

        task = asyncio.create_task(sender())
        try:
            async for m in ws:
                if isinstance(m, str):
                    print(m)
                else:
                    sid, seq, sr = struct.unpack_from("<III", m, 0)
                    name = f"out_{seq}.wav"
                    with wave.open(name, "wb") as o:
                        o.setnchannels(1); o.setsampwidth(2); o.setframerate(sr)
                        o.writeframes(m[12:])
                    print(f"  ↳ صوت seq={seq} {(len(m) - 12) / 2 / sr:.2f}s محفوظ في {name}")
        finally:
            task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
