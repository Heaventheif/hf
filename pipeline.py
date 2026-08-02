from typing import Dict, Any, Optional
from io import BytesIO
import asyncio
import requests
from PIL import Image
import numpy as np
import cv2

from ocr_engines import ocr_image_paddle
from translators import translate_to_ar

def fetch_image(url: str) -> Image.Image:
    # FIX: أُزيل stream=True لأنه لا فائدة منه مع r.content (يُحمَّل كله على أي حال)
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    img = Image.open(BytesIO(r.content))
    # FIX: تحويل RGBA/P/L كلها إلى RGB بأمان
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img

def preprocess_for_ocr(pil_img: Image.Image) -> Image.Image:
    img = np.array(pil_img)  # RGB uint8

    r, g, b = img[:, :, 0], img[:, :, 1], img[:, :, 2]
    colorfulness = (np.std(r.astype(float)) + np.std(g.astype(float)) + np.std(b.astype(float))) / 3.0
    is_color = colorfulness > 15

    if is_color:
        lab = cv2.cvtColor(img, cv2.COLOR_RGB2LAB)
        gray = lab[:, :, 0]          # uint8 دائماً - bilateralFilter يقبله
    else:
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

    gray = cv2.bilateralFilter(gray, d=5, sigmaColor=50, sigmaSpace=50)

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray2 = clahe.apply(gray)

    _, bw = cv2.threshold(gray2, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    kernel = np.ones((2, 2), np.uint8)
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, kernel, iterations=1)

    white_ratio = float(np.mean(bw == 255))
    if white_ratio < 0.15:
        bw = 255 - bw

    return Image.fromarray(bw)

def detect_language_stub(_text: str) -> Optional[str]:
    return None

async def process_image_url(url: str) -> Dict[str, Any]:
    loop = asyncio.get_event_loop()
    try:
        # FIX: العمليات الثقيلة (I/O + CPU) تُشغَّل في executor
        # حتى لا تبلوك event loop عند معالجة صفحات متعددة
        img  = await loop.run_in_executor(None, fetch_image, url)
        prep = await loop.run_in_executor(None, preprocess_for_ocr, img)

        ocr_text = await loop.run_in_executor(None, ocr_image_paddle, prep)
        src_lang  = detect_language_stub(ocr_text)

        translated = await loop.run_in_executor(
            None, translate_to_ar, ocr_text, src_lang
        )

        return {
            "url": url,
            "source_language": src_lang,
            "ocr_text": ocr_text,
            "translated_text": translated,
            "error": None,
        }
    except Exception as e:
        return {
            "url": url,
            "source_language": None,
            "ocr_text": None,
            "translated_text": None,
            "error": str(e),
        }
