"""File exporters — JSON, CSV."""
from __future__ import annotations

import csv
import json
import os
from typing import Any, Dict, Iterable, List

from .config import settings


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def flatten_pin(pin) -> Dict[str, Any]:
    return {
        "id": pin.id,
        "url": str(pin.url) if pin.url else None,
        "title": pin.title,
        "image_original": (pin.image or {}).get("original"),
        "image_src": (pin.image or {}).get("src"),
        "source": pin.source,
    }


def to_json(filename: str, payload: Any) -> str:
    full = os.path.join(settings.data_dir, filename)
    _ensure_dir(os.path.dirname(full) or ".")
    with open(full, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    return full


def to_csv(filename: str, rows: Iterable[Any]) -> str:
    rows = list(rows)
    full = os.path.join(settings.data_dir, filename)
    _ensure_dir(os.path.dirname(full) or ".")
    if not rows:
        with open(full, "w", encoding="utf-8") as f:
            f.write("")
        return full
    fieldnames = list(rows[0].keys())
    with open(full, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return full
