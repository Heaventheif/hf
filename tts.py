"""Piper TTS + تنظيف النص العربي (أرقام → كلمات، إزالة الرموز) + كاش."""
import hashlib
import json
import logging
import re
import threading

from piper import PiperVoice, SynthesisConfig

import config as C
from cache import LRUCache

log = logging.getLogger("ytdub.tts")

# ------------------------------------------------------------ أرقام → كلمات
_ONES = ["صفر", "واحد", "اثنان", "ثلاثة", "أربعة", "خمسة", "ستة", "سبعة", "ثمانية", "تسعة",
         "عشرة", "أحد عشر", "اثنا عشر", "ثلاثة عشر", "أربعة عشر", "خمسة عشر", "ستة عشر",
         "سبعة عشر", "ثمانية عشر", "تسعة عشر"]
_TENS = ["", "", "عشرون", "ثلاثون", "أربعون", "خمسون", "ستون", "سبعون", "ثمانون", "تسعون"]
_HUND = ["", "مئة", "مئتان", "ثلاثمئة", "أربعمئة", "خمسمئة", "ستمئة", "سبعمئة", "ثمانمئة", "تسعمئة"]
_SCALES = [(10 ** 9, ("مليار", "ملياران", "مليارات")),
           (10 ** 6, ("مليون", "مليونان", "ملايين")),
           (10 ** 3, ("ألف", "ألفان", "آلاف"))]
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _below_1000(n: int) -> str:
    parts = []
    h, r = divmod(n, 100)
    if h:
        parts.append(_HUND[h])
    if r:
        if r < 20:
            parts.append(_ONES[r])
        else:
            t, o = divmod(r, 10)
            if o:
                parts.append(_ONES[o])
            parts.append(_TENS[t])
    return " و".join(parts)


def int_to_words(n: int) -> str:
    if n == 0:
        return _ONES[0]
    if n >= 10 ** 12:
        return " ".join(_ONES[int(d)] for d in str(n))
    parts = []
    for value, (one, two, few) in _SCALES:
        q, n = divmod(n, value)
        if not q:
            continue
        if q == 1:
            parts.append(one)
        elif q == 2:
            parts.append(two)
        elif q <= 10:
            parts.append(f"{_below_1000(q)} {few}")
        else:
            parts.append(f"{_below_1000(q)} {one}")
    if n:
        parts.append(_below_1000(n))
    return " و".join(parts)


def _num_repl(m):
    whole, frac = m.group(1), m.group(2)
    w = int_to_words(int(whole))
    if frac:
        w += " فاصلة " + " ".join(_ONES[int(d)] for d in frac)
    return " " + w + " "


_STRIP = re.compile(r"[\*\#_~\^\|<>\{\}\[\]\(\)\"“”«»`@\\/=+•·\u200e\u200f\u202a-\u202e]")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")


def prepare_arabic(text: str) -> str:
    text = text.translate(_AR_DIGITS)
    text = re.sub(r"(\d+)\s*%", r"\1 في المئة", text)
    text = re.sub(r"(\d+)(?:[.,](\d+))?", _num_repl, text)
    text = _EMOJI.sub(" ", text)
    text = _STRIP.sub(" ", text)
    text = text.replace("&", " و ").replace(",", "،").replace("?", "؟")
    return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------------ Piper
class TTS:
    def __init__(self):
        self.voices = {}
        for onnx in sorted(C.VOICES_DIR.glob("*.onnx")):
            cfg = onnx.parent / (onnx.name + ".json")
            if not cfg.exists():
                continue
            sr = json.loads(cfg.read_text(encoding="utf-8"))["audio"]["sample_rate"]
            vid = onnx.name[:-len(".onnx")]
            self.voices[vid] = {"voice": PiperVoice.load(onnx, config_path=cfg), "sr": sr,
                                "cps": 13.0, "lock": threading.Lock()}
            log.info("loaded voice %s (%d Hz)", vid, sr)
        if not self.voices:
            raise RuntimeError("لا توجد أصوات Piper في " + str(C.VOICES_DIR))
        self.cache = LRUCache(C.CACHE_MAX_BYTES)

    def has(self, vid: str) -> bool:
        return vid in self.voices

    def list_voices(self):
        out = []
        for vid, v in self.voices.items():
            parts = vid.split("-")
            name = parts[1].title() if len(parts) > 1 else vid
            quality = parts[2] if len(parts) > 2 else ""
            out.append({"id": vid, "name": f"{name} ({quality})" if quality else name,
                        "sample_rate": v["sr"]})
        return out

    def plan_length_scale(self, text: str, vid: str, target_sec: float) -> float:
        """يقدّر length_scale ليتطابق طول الكلام مع زمن المقطع الأصلي."""
        v = self.voices[vid]
        if target_sec < 0.8:
            return 1.0
        predicted = max(0.3, len(text) / v["cps"])
        return max(C.LS_MIN, min(C.LS_MAX, target_sec / predicted))

    def synthesize(self, text: str, vid: str, length_scale: float = 1.0):
        """يعيد (PCM16 bytes، sample_rate). النص يُنظَّف داخلياً."""
        v = self.voices[vid]
        text = prepare_arabic(text)
        if not text:
            return b"", v["sr"]
        ls = round(length_scale * 20) / 20
        key = hashlib.sha1(f"{vid}|{ls:.2f}|{text}".encode()).hexdigest()
        hit = self.cache.get(key)
        if hit is not None:
            return hit, v["sr"]
        cfg = SynthesisConfig(length_scale=ls, normalize_audio=True)
        with v["lock"]:
            pcm = b"".join(c.audio_int16_bytes for c in v["voice"].synthesize(text, syn_config=cfg))
        dur = len(pcm) / 2 / v["sr"]
        if dur > 0.2:                                      # تحديث معدّل الكلام المقدّر
            v["cps"] = 0.8 * v["cps"] + 0.2 * (len(text) * ls / dur)
        self.cache.put(key, pcm)
        return pcm, v["sr"]
