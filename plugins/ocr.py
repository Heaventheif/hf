"""
plugins/ocr.py — Plugin للـ OCR والترجمة (المنقول من s الأصلي).

يوفر endpoint واحد:
  POST /ocr/infer  — يستقبل قائمة روابط صور، يعيد نص OCR + ترجمة عربية.

نقطة التوسع: لإضافة لغة جديدة أو محرك OCR مختلف، عدِّل هذا الملف فقط
أو أضف plugin جديد كلياً — لا تعديل في main.py.
"""
from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from typing import Any, Optional

import cv2
import numpy as np
import requests
from fastapi import HTTPException
from PIL import Image
from pydantic import BaseModel, HttpUrl

from internal.plugin import Plugin, Route

log = logging.getLogger(__name__)

# ─── نماذج Pydantic ──────────────────────────────────────────────────────────

class InferRequest(BaseModel):
    image_urls: Optional[list[HttpUrl]] = None
    pages: Optional[list[dict[str, Any]]] = None


class InferItem(BaseModel):
    url: str
    source_language: Optional[str] = None
    ocr_text: Optional[str] = None
    translated_text: Optional[str] = None
    error: Optional[str] = None


class InferResponse(BaseModel):
    results: list[InferItem]


# ─── OCR engine (lazy, singleton) ────────────────────────────────────────────

_ocr_engine = None


def _get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from paddleocr import PaddleOCR
        _ocr_engine = PaddleOCR(use_angle_cls=True, lang="japan")
        log.info("[ocr] PaddleOCR engine جاهز (lang=japan)")
    return _ocr_engine


def _ocr_image(pil_img: Image.Image) -> str:
    engine = _get_ocr()
    np_img = np.array(pil_img)
    result = engine.ocr(np_img, cls=True)
    if not result:
        return ""
    lines = []
    for block in result:
        if not block:
            continue
        for item in block:
            if not item or len(item) < 2:
                continue
            conf_tuple = item[1]
            if conf_tuple and conf_tuple[0]:
                lines.append(conf_tuple[0])
    return "\n".join(lines).replace("\u3000", " ").strip()


# ─── Translation model (lazy, singleton) ─────────────────────────────────────

_tokenizer = None
_model = None
_AR_CODE = "arb_Arab"

_MODEL_CANDIDATES = [
    "facebook/nllb-200-distilled-600M",
    "facebook/nllb-200-3.3B",
]


def _load_translation_model():
    global _tokenizer, _model
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    last_err = None
    for name in _MODEL_CANDIDATES:
        try:
            _tokenizer = AutoTokenizer.from_pretrained(name)
            _model = AutoModelForSeq2SeqLM.from_pretrained(name)
            log.info("[ocr] Translation model جاهز: %s", name)
            return
        except Exception as exc:
            last_err = exc
            log.warning("[ocr] فشل تحميل %s: %s", name, exc)
    raise RuntimeError(f"تعذّر تحميل أي نموذج ترجمة: {last_err}")


def _translate(text: str, src_lang: Optional[str] = None) -> str:
    if not text or not text.strip():
        return ""
    global _tokenizer, _model
    if _tokenizer is None or _model is None:
        _load_translation_model()

    import re
    t = re.sub(r"[ \t]+", " ", text).strip()
    if src_lang:
        _tokenizer.src_lang = src_lang

    inputs = _tokenizer(t, return_tensors="pt", truncation=True, max_length=512)

    target_id = None
    if hasattr(_tokenizer, "lang_code_to_id") and _AR_CODE in _tokenizer.lang_code_to_id:
        target_id = _tokenizer.lang_code_to_id[_AR_CODE]
    else:
        tid = _tokenizer.convert_tokens_to_ids(_AR_CODE)
        if tid != _tokenizer.unk_token_id:
            target_id = tid

    gen_kwargs: dict = dict(max_new_tokens=256, num_beams=4, do_sample=False)
    if target_id is not None:
        gen_kwargs["forced_bos_token_id"] = target_id

    out = _model.generate(**inputs, **gen_kwargs)
    return _tokenizer.batch_decode(out, skip_special_tokens=True)[0].strip()


# ─── Image pipeline ───────────────────────────────────────────────────────────

def _fetch_image(url: str) -> Image.Image:
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    img = Image.open(BytesIO(r.content))
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _preprocess(pil_img: Image.Image) -> Image.Image:
    img = np.array(pil_img)
    r, g, b = img[:, :, 0], img[:, :, 1], img[:, :, 2]
    colorfulness = (np.std(r.astype(float)) + np.std(g.astype(float)) + np.std(b.astype(float))) / 3.0

    if colorfulness > 15:
        lab = cv2.cvtColor(img, cv2.COLOR_RGB2LAB)
        gray = lab[:, :, 0]
    else:
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

    gray = cv2.bilateralFilter(gray, d=5, sigmaColor=50, sigmaSpace=50)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray2 = clahe.apply(gray)
    _, bw = cv2.threshold(gray2, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    kernel = np.ones((2, 2), np.uint8)
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, kernel, iterations=1)

    if float(np.mean(bw == 255)) < 0.15:
        bw = 255 - bw
    return Image.fromarray(bw)


async def _process_one(url: str) -> dict:
    loop = asyncio.get_event_loop()
    try:
        img = await loop.run_in_executor(None, _fetch_image, url)
        prep = await loop.run_in_executor(None, _preprocess, img)
        ocr_text = await loop.run_in_executor(None, _ocr_image, prep)
        translated = await loop.run_in_executor(None, _translate, ocr_text, None)
        return {"url": url, "source_language": None, "ocr_text": ocr_text, "translated_text": translated, "error": None}
    except Exception as exc:
        return {"url": url, "source_language": None, "ocr_text": None, "translated_text": None, "error": str(exc)}


# ─── Handler ──────────────────────────────────────────────────────────────────

async def _handle_infer(req: InferRequest) -> InferResponse:
    urls: list[str] = []
    if req.image_urls:
        urls = [str(u) for u in req.image_urls]
    elif req.pages:
        urls = [str(p["url"]) for p in req.pages if isinstance(p, dict) and "url" in p]
    else:
        raise HTTPException(status_code=400, detail="Provide image_urls or pages[].url")

    if not urls:
        raise HTTPException(status_code=400, detail="No image URLs found")

    results = await asyncio.gather(*[_process_one(u) for u in urls])
    return InferResponse(results=[InferItem(**r) for r in results])


# ─── Plugin definition ────────────────────────────────────────────────────────

class OcrPlugin(Plugin):
    description = "OCR (PaddleOCR — Japanese/Multi) + ترجمة عربية (NLLB)"

    requirements = [
        "requests",
        "pillow",
        "opencv-python",
        "numpy",
        "paddlepaddle",
        "paddleocr",
        "transformers",
        "sentencepiece",
        "protobuf",
        # torch يأتي عبر pip_extra (يحتاج --extra-index-url)
        "torch",
    ]
    pip_extra = [
        "--extra-index-url https://download.pytorch.org/whl/cpu",
    ]

    def name(self) -> str:
        return "ocr"

    def routes(self) -> list[Route]:
        return [Route(method="POST", path="/ocr/infer", handler=_handle_infer)]

    def startup(self) -> None:
        """تحميل مسبق للنماذج عند البدء — يمنع بطء أول طلب."""
        try:
            log.info("[ocr] جاري تحميل النماذج مسبقاً...")
            _get_ocr()
            _load_translation_model()
            log.info("[ocr] ✅ النماذج جاهزة")
        except Exception as exc:
            log.warning("[ocr] ⚠️  تعذّر التحميل المسبق: %s", exc)

    def status(self) -> dict:
        return {
            "status": "loaded",
            "ocr_engine": "ready" if _ocr_engine is not None else "not_loaded",
            "translation_model": "ready" if _model is not None else "not_loaded",
        }


# المتغير الوحيد الذي يقرأه loader.py
plugin = OcrPlugin()
