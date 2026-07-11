"""
plugins/img_translate.py
────────────────────────────────────────────────────────────────
نفس منطق server.py الأصلي (Sanic) لكن محوَّل بالكامل ليعمل كـ plugin
داخل الـ FastAPI app الموحّد لمساحة hf (main.py الثابت + plugin_loader).

نقطة النهاية:
    POST /img_tr
يقبل:
  1) multipart/form-data — عدة حقول باسم "images" (ملفات ثنائية خام) — الموصى به.
  2) application/json — {"images": ["<base64>", "data:image/png;base64,..."]}
     (توافق خلفي فقط).

الإخراج: بروتوكول تأطير ثنائي بسيط (نفس بروتوكول النسخة الأصلية تماماً)
يُبَث كـ application/octet-stream:
    [4 bytes big-endian uint32]  = طول ترويسة JSON التالية
    [N bytes]                    = ترويسة JSON UTF-8:
         {"index": 0, "status": "success", "error": null,
          "format": "png", "length": 123456}
    [length bytes]                = بيانات الصورة الثنائية الخام (0 إن كان error)
  وفي النهاية إطار ختامي {"done": true} بطول بيانات = 0.

الحماية: محمي تلقائياً بنفس middleware التوكن السري (X-Internal-Token)
المُفعّل مركزياً في plugin_loader.py — لا حاجة لأي كود إضافي هنا.

الخط العربي: نعتمد على حزمة نظام (fonts-noto-naskh-arabic) بدل ملف محلي
يدوي التحميل، فتُضاف تلقائياً لـ Dockerfile عبر DOCKERFILE_DEPS أدناه.
لو الحزمة غير متاحة لأي سبب، نبحث عن أي خط عربي بديل مثبت على النظام.
"""

import asyncio
import glob
import io
import json
import logging
import re
import struct
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np
import pytesseract
import requests
from PIL import Image, ImageDraw, ImageFont
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

import arabic_reshaper
from bidi.algorithm import get_display

logger = logging.getLogger("img_translate")

DESCRIPTION = "ترجمة صور (OCR + مسح ذكي + رسم عربي مُشكَّل) وبثّها كإطارات ثنائية عبر /img_tr"

# apt packages يضيفها plugin_loader تلقائياً لـ Dockerfile إن لم تكن موجودة
DOCKERFILE_DEPS = [
    "tesseract-ocr",
    "tesseract-ocr-eng",
    "tesseract-ocr-jpn",
    "tesseract-ocr-chi-sim",
    "fonts-noto-naskh-arabic",
]

# ================== الإعدادات العامة ==================

WORKER_POOL_SIZE = 3
TESS_LANGS = "jpn+eng+chi_sim"
TESS_PSM = 11
MAX_IMAGE_BYTES = 25 * 1024 * 1024  # 25MB لكل صورة

_executor = ThreadPoolExecutor(max_workers=WORKER_POOL_SIZE, thread_name_prefix="img_translate")

_DATA_URI_RE = re.compile(r"^data:.*?;base64,", re.IGNORECASE)


def _find_arabic_font() -> Optional[str]:
    """يبحث عن خط عربي مثبت على النظام (fonts-noto-naskh-arabic أولاً،
    ثم أي بديل مناسب متاح)."""
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
    ]
    for c in candidates:
        if glob.glob(c):
            return c
    for pattern in (
        "/usr/share/fonts/**/*Naskh*Arabic*.ttf",
        "/usr/share/fonts/**/*Arabic*.ttf",
        "/usr/share/fonts/**/*noto*rabic*.ttf",
    ):
        found = glob.glob(pattern, recursive=True)
        if found:
            return found[0]
    return None


FONT_PATH = _find_arabic_font()
if FONT_PATH:
    logger.info(f"[img_translate] استخدام الخط العربي: {FONT_PATH}")
else:
    logger.warning(
        "[img_translate] ⚠️ لم يُعثر على أي خط عربي مثبت — سيتم تخطي رسم النص "
        "العربي (تأكد من إضافة fonts-noto-naskh-arabic في Dockerfile)."
    )

router = APIRouter(tags=["img-translate"])


# ================== نقطة النهاية الرئيسية (بث ثنائي) ==================

@router.post("/img_tr")
async def img_translate_stream(request: Request):
    images_raw: list[bytes] = []
    content_type = (request.headers.get("content-type") or "").lower()

    if "multipart/form-data" in content_type:
        form = await request.form()
        files = form.getlist("images")
        if not files:
            return _error_response("لم يتم إرسال أي ملفات في الحقل 'images'", 400)
        for f in files:
            body = await f.read()
            if len(body) > MAX_IMAGE_BYTES:
                return _error_response("حجم إحدى الصور يتجاوز الحد المسموح", 400)
            images_raw.append(body)

    elif "application/json" in content_type:
        try:
            payload = json.loads(await request.body())
        except Exception as e:
            return _error_response(f"جسم JSON غير صالح: {e}", 400)

        items = payload.get("images") or []
        if not items:
            return _error_response("لم يتم إرسال أي صور", 400)

        import base64
        for b64 in items:
            b64_clean = _DATA_URI_RE.sub("", b64)
            try:
                raw = base64.b64decode(b64_clean)
            except Exception as e:
                return _error_response(f"بيانات Base64 غير صالحة: {e}", 400)
            if len(raw) > MAX_IMAGE_BYTES:
                return _error_response("حجم إحدى الصور يتجاوز الحد المسموح", 400)
            images_raw.append(raw)
    else:
        return _error_response(
            "استخدم multipart/form-data (موصى به) أو application/json", 400
        )

    return StreamingResponse(
        _stream_all_images(request, images_raw),
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _error_response(msg: str, status: int):
    return JSONResponse({"error": msg}, status_code=status)


# ================== إدارة البث + Worker Pool + إلغاء عند قطع الاتصال ==================

async def _stream_all_images(request: Request, images_raw: list):
    loop = asyncio.get_event_loop()
    semaphore = asyncio.Semaphore(WORKER_POOL_SIZE)
    queue: asyncio.Queue = asyncio.Queue()
    cancel_event = asyncio.Event()

    async def run_one(index: int, raw: bytes):
        async with semaphore:
            if cancel_event.is_set():
                return
            try:
                result = await loop.run_in_executor(
                    _executor, process_image, index, raw, cancel_event
                )
            except Exception as e:
                result = _frame_dict(index, "error", error=str(e))
            await queue.put(result)

    tasks = [asyncio.create_task(run_one(i, raw)) for i, raw in enumerate(images_raw)]

    remaining = len(tasks)
    get_task = None
    try:
        while remaining > 0:
            if await request.is_disconnected():
                cancel_event.set()
                break

            if get_task is None:
                get_task = asyncio.create_task(queue.get())

            done, _ = await asyncio.wait({get_task}, timeout=0.5)
            if not done:
                continue

            result = get_task.result()
            get_task = None
            remaining -= 1

            yield _encode_frame(result)
    finally:
        if get_task is not None and not get_task.done():
            get_task.cancel()
        if cancel_event.is_set():
            for t in tasks:
                t.cancel()
        else:
            yield _encode_frame({"done": True})


def _frame_dict(index: int, status: str, error: Optional[str] = None,
                 image_bytes: Optional[bytes] = None, fmt: str = "png") -> dict:
    return {
        "index": index,
        "status": status,
        "error": error,
        "format": fmt,
        "_binary": image_bytes,
    }


def _encode_frame(result: dict) -> bytes:
    image_bytes = result.pop("_binary", None) if isinstance(result, dict) else None
    header = dict(result)
    header["length"] = len(image_bytes) if image_bytes else 0

    header_json = json.dumps(header, ensure_ascii=False).encode("utf-8")
    out = struct.pack(">I", len(header_json)) + header_json
    if image_bytes:
        out += image_bytes
    return out


# ================== معالجة صورة واحدة (تعمل داخل ThreadPoolExecutor) ==================

def process_image(index: int, raw: bytes, cancel_event: asyncio.Event) -> dict:
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        img_format = (img.format or "PNG").lower()
        if img_format not in ("png", "jpeg", "jpg"):
            img_format = "png"
    except Exception as e:
        return _frame_dict(index, "error", error=f"تعذر فك ترميز الصورة: {e}")

    if img.mode != "RGB":
        img = img.convert("RGB")

    try:
        ocr_config = f"--psm {TESS_PSM}"
        data = pytesseract.image_to_data(
            img, lang=TESS_LANGS, config=ocr_config,
            output_type=pytesseract.Output.DICT,
        )
    except Exception as e:
        return _frame_dict(index, "error", error=f"فشل التعرف الضوئي (OCR): {e}")

    draw = ImageDraw.Draw(img)
    np_img = np.array(img)

    n_boxes = len(data.get("text", []))
    for i in range(n_boxes):
        if cancel_event.is_set():
            return _frame_dict(index, "error", error="تم إلغاء الطلب أثناء المعالجة")

        text = (data["text"][i] or "").strip()
        if not text:
            continue

        x, y, w, h = (data["left"][i], data["top"][i], data["width"][i], data["height"][i])
        if w < 3 or h < 3:
            continue

        clean_region(draw, np_img, x, y, w, h)

        translated = translate_to_arabic(text)
        if not translated.strip():
            continue

        draw_arabic_text(draw, translated, x, y, w, h)

    buf = io.BytesIO()
    save_format = "PNG" if img_format == "png" else "JPEG"
    if save_format == "JPEG":
        img.save(buf, format="JPEG", quality=92)
    else:
        img.save(buf, format="PNG")

    return _frame_dict(index, "success", image_bytes=buf.getvalue(), fmt=save_format.lower())


# ================== تبييض/تنظيف الخلفية الذكي ==================

def clean_region(draw: ImageDraw.ImageDraw, np_img: np.ndarray,
                  x: int, y: int, w: int, h: int, pad: int = 4, border: int = 6):
    H, W = np_img.shape[0], np_img.shape[1]

    x0, y0 = max(0, x - pad - border), max(0, y - pad - border)
    x1, y1 = min(W, x + w + pad + border), min(H, y + h + pad + border)

    outer = np_img[y0:y1, x0:x1].reshape(-1, 3)

    if outer.size == 0:
        avg = np.array([255, 255, 255])
    else:
        avg = outer.mean(axis=0)

    r, g, b = avg[0], avg[1], avg[2]
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    is_near_white = luminance > 235 and abs(r - g) < 12 and abs(g - b) < 12

    fill_color = (255, 255, 255) if is_near_white else (int(r), int(g), int(b))

    box_x0, box_y0 = max(0, x - pad), max(0, y - pad)
    box_x1, box_y1 = min(W, x + w + pad), min(H, y + h + pad)
    draw.rectangle([box_x0, box_y0, box_x1, box_y1], fill=fill_color)


# ================== كتابة النص العربي (تشكيل + BiDi + التفاف + توسيط) ==================

def draw_arabic_text(draw: ImageDraw.ImageDraw, text: str, x: int, y: int, w: int, h: int):
    if not FONT_PATH or w < 4 or h < 4:
        return

    reshaped = arabic_reshaper.reshape(text)
    bidi_text = get_display(reshaped)

    max_width = w * 0.95
    font_size = max(8, int(h * 0.72))
    min_font_size = 8

    font = None
    lines = []
    while font_size >= min_font_size:
        try:
            font = ImageFont.truetype(FONT_PATH, font_size)
        except Exception:
            return
        lines = _wrap_text(draw, bidi_text, font, max_width)
        line_height = font_size * 1.25
        total_height = line_height * len(lines)
        if total_height <= h * 1.15:
            break
        font_size -= 1

    if font is None:
        return

    line_height = font_size * 1.25
    total_height = line_height * len(lines)
    start_y = y + (h - total_height) / 2

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        line_x = x + (w - line_w) / 2
        line_y = start_y + i * line_height
        draw.text((line_x, line_y), line, font=font, fill=(0, 0, 0))


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
               max_width: float) -> list[str]:
    words = text.split(" ")
    lines: list[str] = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        bbox = draw.textbbox((0, 0), candidate, font=font)
        width = bbox[2] - bbox[0]
        if width <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word

    if current:
        lines.append(current)
    return lines or [text]


# ================== الترجمة المجانية عبر Google Translate ==================

def translate_to_arabic(text: str) -> str:
    if not text.strip():
        return ""
    try:
        resp = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "ar", "dt": "t", "q": text},
            headers={"User-Agent": "Mozilla/5.0 (compatible; ImgTranslateBot/1.0)"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return "".join(seg[0] for seg in data[0] if seg and seg[0])
    except Exception:
        return ""


# ================== التسجيل في التطبيق ==================

def register(app):
    app.include_router(router)
