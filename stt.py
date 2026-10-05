from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel


class SpeechToText:
    def __init__(self, model_dir: Path, model_name: str = "tiny", device: str = "cpu", compute_type: str = "int8"):
        self.model_dir = model_dir
        self.device = device
        self.compute_type = compute_type
        self._lock = threading.Lock()
        self._models: dict[str, WhisperModel] = {}
        self._active = model_name
        self._load(model_name)

    def _load(self, name: str) -> WhisperModel:
        if name not in self._models:
            with self._lock:
                if name not in self._models:
                    self._models[name] = WhisperModel(str(self.model_dir), device=self.device, compute_type=self.compute_type)
        return self._models[name]

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> tuple[str, str, float]:
        model = self._load(self._active)
        segments, info = model.transcribe(audio, language=language, vad_filter=True, beam_size=1, condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, info.language or language or "auto", float(info.language_probability or 0.0)

    @property
    def active_model(self) -> str:
        return self._active
