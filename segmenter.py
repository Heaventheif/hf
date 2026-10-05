from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class AudioSegment:
    pcm: np.ndarray
    start_sample: int
    end_sample: int


class Segmenter:
    """Energy VAD مناسب للبث PCM16؛ Whisper يقوم بـ VAD أدق داخل كل مقطع."""

    def __init__(self, sample_rate: int = 16_000, max_seconds: float = 8.0, silence_seconds: float = 0.35):
        self.sample_rate = sample_rate
        self.max_samples = int(max_seconds * sample_rate)
        self.silence_samples = int(silence_seconds * sample_rate)
        self.buffer = np.empty(0, dtype=np.float32)
        self.start_sample = 0
        self.silent = 0

    def push(self, pcm16: bytes, offset: int) -> list[AudioSegment]:
        if not pcm16 or len(pcm16) % 2:
            return []
        data = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        if self.buffer.size == 0:
            self.start_sample = offset
        self.buffer = np.concatenate((self.buffer, data))
        rms = float(np.sqrt(np.mean(data * data))) if data.size else 0.0
        self.silent = self.silent + data.size if rms < 0.012 else 0
        if self.buffer.size >= self.max_samples or (self.buffer.size >= self.sample_rate // 2 and self.silent >= self.silence_samples):
            return self.flush()
        return []

    def flush(self) -> list[AudioSegment]:
        if self.buffer.size < self.sample_rate // 5:
            return []
        result = [AudioSegment(self.buffer, self.start_sample, self.start_sample + self.buffer.size)]
        self.buffer = np.empty(0, dtype=np.float32)
        self.silent = 0
        return result
