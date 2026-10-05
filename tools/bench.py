"""Phase 0 / الاختبار D: قياس RTF لكل مرحلة على CPU الخاص بالـ Space.
التشغيل (من مجلد space، بعد تنزيل النماذج):  python tools/bench.py [--wav clip.wav]
"""
import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stt import STT          # noqa: E402
from translator import Translator   # noqa: E402
from tts import TTS          # noqa: E402

TEXT = ("The weather is nice today, so we decided to go for a long walk by the river "
        "and talk about the project.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", help="ملف 16kHz mono PCM16 (اختياري)")
    ap.add_argument("--text", default=TEXT)
    a = ap.parse_args()

    stt = STT()
    mt = Translator(stt.device)
    tts = TTS()
    voice = next(iter(tts.voices))

    t = time.perf_counter(); ar = mt.translate(a.text, "en", "ar"); t_mt = time.perf_counter() - t
    t = time.perf_counter(); pcm, sr = tts.synthesize(ar, voice, 1.0); t_tts = time.perf_counter() - t
    dur_tts = len(pcm) / 2 / sr
    print(f"MT : {t_mt:.2f}s  -> {ar}")
    print(f"TTS: {t_tts:.2f}s for {dur_tts:.1f}s audio (RTF {t_tts / dur_tts:.2f})")

    if a.wav:
        with wave.open(a.wav) as w:
            assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
            audio = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    else:
        x = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768
        n = int(len(x) * 16000 / sr)
        audio = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
        print("(لا يوجد --wav: استُخدم صوت Piper العربي لقياس STT)")
    dur = len(audio) / 16000
    for lvl in stt.models:
        stt.level = lvl
        stt.transcribe(audio[:16000], None)                       # إحماء
        t = time.perf_counter(); stt.transcribe(audio, None); t_stt = time.perf_counter() - t
        total = t_stt + t_mt + t_tts
        print(f"STT[{lvl:5}] {t_stt:.2f}s for {dur:.1f}s (RTF {t_stt / dur:.2f}) | "
              f"STT+MT+TTS ≈ {total:.2f}s")
    print("المعيار: المجموع < 0.8 × مدة المقطع (5 ثوانٍ → أقل من 4 ثوانٍ).")


if __name__ == "__main__":
    main()
