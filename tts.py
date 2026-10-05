from __future__ import annotations

from pathlib import Path

from piper import PiperVoice
from piper.config import SynthesisConfig


class ArabicTTS:
    def __init__(self, model_path: Path):
        self.model_path = model_path
        self.voice = PiperVoice.load(model_path)
        self.sample_rate = int(self.voice.config.sample_rate)

    def synthesize(self, text: str, length_scale: float = 1.0) -> bytes:
        scale = max(0.8, min(1.15, float(length_scale)))
        config = SynthesisConfig(length_scale=scale)
        return b"".join(chunk.audio_int16_bytes for chunk in self.voice.synthesize(text, syn_config=config))
