"""تقطيع بث PCM16 (16kHz) إلى مقاطع كلام باستخدام كشف طاقة متكيّف.

القطع عند وقفة >= MIN_SILENCE_SEC، وبحد أقصى MAX_UTT_SEC (القطع القسري يتم
عند أهدأ إطار في آخر 1.5 ثانية حتى لا تُقطع الكلمات).
"""
from collections import deque
from dataclasses import dataclass

import numpy as np

import config as C

FRAME = int(C.SR * 0.03)                   # 30ms
FRAME_SEC = FRAME / C.SR
PAD_FRAMES = int(round(C.PAD_SEC / FRAME_SEC))
SIL_FRAMES = int(round(C.MIN_SILENCE_SEC / FRAME_SEC))
MAX_FRAMES = int(C.MAX_UTT_SEC / FRAME_SEC)
CUT_WINDOW = int(1.2 / FRAME_SEC)
MIN_CUT = int(2.0 / FRAME_SEC)


@dataclass
class Utterance:
    start: int            # فهرس العيّنة داخل الجلسة
    end: int
    audio: np.ndarray     # float32 [-1, 1]

    @property
    def duration(self) -> float:
        return (self.end - self.start) / C.SR


class Segmenter:
    def __init__(self):
        self.reset(0)

    def reset(self, offset: int = 0):
        self.expected = offset
        self.pos = offset                   # بداية الإطار التالي
        self.carry = np.zeros(0, np.int16)
        self.noise = 0.004
        self.in_speech = False
        self.frames: list = []
        self.rms: list = []
        self.utt_start = offset
        self.silence_run = 0
        self.voiced = 0
        self.pre: deque = deque(maxlen=PAD_FRAMES)   # (start, frame, rms)

    # ---------------------------------------------------------------- API
    def feed(self, pcm: np.ndarray, offset: int) -> list:
        out = []
        if offset != self.expected:          # انقطاع في التسلسل
            out += self.flush()
            self.reset(offset)
        self.expected = offset + len(pcm)
        data = np.concatenate([self.carry, pcm]) if len(self.carry) else pcm
        n = len(data) // FRAME
        for i in range(n):
            out += self._frame(data[i * FRAME:(i + 1) * FRAME])
        self.carry = data[n * FRAME:]
        return out

    def flush(self) -> list:
        out = []
        if self.in_speech and self.frames:
            u = self._make(len(self.frames))
            if u:
                out.append(u)
        self.in_speech = False
        self.frames, self.rms = [], []
        return out

    def frontier_sample(self) -> int:
        """أقدم عيّنة قد تنتمي لمقطع لم يكتمل بعد."""
        if self.in_speech:
            return self.utt_start
        return self.pre[0][0] if self.pre else self.pos

    # ------------------------------------------------------------ internals
    def _make(self, end_idx: int):
        if self.voiced * FRAME_SEC < C.MIN_SPEECH_SEC or end_idx <= 0:
            return None
        audio = np.concatenate(self.frames[:end_idx]).astype(np.float32) / 32768.0
        return Utterance(self.utt_start, self.utt_start + end_idx * FRAME, audio)

    def _frame(self, fr: np.ndarray) -> list:
        x = fr.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x))) + 1e-9
        start = self.pos
        self.pos += FRAME
        hi = max(self.noise * 3.0, 0.008)
        lo = max(self.noise * 1.8, 0.005)

        if not self.in_speech:
            if rms > hi:
                pre = list(self.pre)
                self.pre.clear()
                self.in_speech = True
                self.frames = [f for (_, f, _) in pre] + [fr]
                self.rms = [r for (_, _, r) in pre] + [rms]
                self.utt_start = pre[0][0] if pre else start
                self.silence_run = 0
                self.voiced = 1
            else:
                self.noise = 0.98 * self.noise + 0.02 * min(rms, 0.05)
                self.pre.append((start, fr, rms))
            return []

        self.frames.append(fr)
        self.rms.append(rms)
        if rms > lo:
            self.silence_run = 0
            self.voiced += 1
        else:
            self.silence_run += 1

        out = []
        if self.silence_run >= SIL_FRAMES:
            end_idx = max(1, min(len(self.frames), len(self.frames) - self.silence_run + PAD_FRAMES))
            u = self._make(end_idx)
            if u:
                out.append(u)
            for i in range(end_idx, len(self.frames)):       # الذيل يصبح حشواً للمقطع التالي
                self.pre.append((self.utt_start + i * FRAME, self.frames[i], self.rms[i]))
            self.in_speech = False
            self.frames, self.rms = [], []
        elif len(self.frames) >= MAX_FRAMES:
            lo_i = max(MIN_CUT, len(self.frames) - CUT_WINDOW)
            cut = lo_i + int(np.argmin(self.rms[lo_i:])) + 1
            u = self._make(cut)
            if u:
                out.append(u)
            # معايرة ضجيج الخلفية (موسيقى/ضوضاء مستمرة)
            self.noise = min(0.05, max(self.noise, 0.5 * float(np.percentile(self.rms, 10))))
            self.utt_start += cut * FRAME
            self.frames = self.frames[cut:]
            self.rms = self.rms[cut:]
            self.voiced = len(self.frames)
            self.silence_run = 0
        return out
