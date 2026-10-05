from __future__ import annotations

import os
from pathlib import Path

from huggingface_hub import snapshot_download


ROOT = Path("/models")
ROOT.mkdir(parents=True, exist_ok=True)

snapshot_download(
    "Systran/faster-whisper-tiny",
    local_dir=ROOT / "whisper",
    allow_patterns=["*.bin", "*.json", "*.txt", "*.model"],
)
snapshot_download(
    "JustFrederik/nllb-200-distilled-600M-ct2-int8",
    local_dir=ROOT / "nllb",
)
snapshot_download(
    "rhasspy/piper-voices",
    local_dir=ROOT / "piper",
    allow_patterns=[
        "ar/ar_JO/kareem/medium/ar_JO-kareem-medium.onnx",
        "ar/ar_JO/kareem/medium/ar_JO-kareem-medium.onnx.json",
    ],
)

# Piper expects the two model files in one directory.
piper_source = ROOT / "piper" / "ar" / "ar_JO" / "kareem" / "medium"
piper_dir = ROOT / "piper"
for name in ("ar_JO-kareem-medium.onnx", "ar_JO-kareem-medium.onnx.json"):
    source = piper_source / name
    target = piper_dir / name
    if source.exists() and not target.exists():
        target.symlink_to(source)

print("Models downloaded successfully")
