from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    port: int = int(os.getenv("PORT", "7860"))
    whisper_model: str = os.getenv("WHISPER_MODEL", "tiny").strip()
    whisper_device: str = os.getenv("WHISPER_DEVICE", "cpu").strip()
    whisper_compute: str = os.getenv("WHISPER_COMPUTE_TYPE", "int8").strip()
    max_in_flight: int = int(os.getenv("MAX_IN_FLIGHT", "2"))
    debug: bool = os.getenv("DEBUG", "false").lower() == "true"
    whisper_dir: Path = Path(os.getenv("WHISPER_MODEL_DIR", "/models/whisper"))
    translation_dir: Path = Path(os.getenv("TRANSLATION_MODEL_DIR", "/models/nllb"))
    piper_dir: Path = Path(os.getenv("PIPER_MODEL_DIR", "/models/piper"))

    @property
    def piper_model(self) -> Path:
        return self.piper_dir / "ar_JO-kareem-medium.onnx"


settings = Settings()
