from typing import Optional
import numpy as np
from PIL import Image

from paddleocr import PaddleOCR

# lang="japan" للمانغا اليابانية - "multi" غير موجود في PaddleOCR
# إذا أردت عربي/صيني غيّره لـ "ch" أو "en"
ocr_engine: Optional[PaddleOCR] = None

def get_engine():
    global ocr_engine
    if ocr_engine is None:
        ocr_engine = PaddleOCR(use_angle_cls=True, lang="japan")
    return ocr_engine

def ocr_image_paddle(pil_img: Image.Image) -> str:
    engine = get_engine()
    np_img = np.array(pil_img)
    result = engine.ocr(np_img, cls=True)

    # FIX: result قد يكون None أو [[]] - يجب التحقق
    if not result:
        return ""

    lines = []
    for block in result:
        if not block:          # block قد يكون None داخل القائمة
            continue
        for item in block:
            if not item or len(item) < 2:
                continue
            conf_tuple = item[1]
            if not conf_tuple or len(conf_tuple) < 1:
                continue
            text = conf_tuple[0]
            if text:
                lines.append(text)

    return "\n".join(lines).replace("\u3000", " ").strip()
