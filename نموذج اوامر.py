#!/usr/bin/env python3
"""
نموذج عام لكشط موقع باستخدام AntiDetectionBrowser (بدون بروكسي).
قم بتعديل دوال extract_text و extract_media حسب احتياجاتك.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import List, Dict
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from browser_engine import AntiDetectionBrowser, create_curl_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scraper")

# ═══════════════════════ إعدادات ═══════════════════════
TARGET_URL = "https://example.com"      # غيّر إلى الرابط المطلوب
HEADLESS = True                         # اجعل True إذا كنت لا تريد رؤية المتصفح
DOWNLOAD_MEDIA = True                   # هل تريد تحميل الصور والفيديوهات؟
SAVE_DIR = Path("./scraped_data")       # مجلد حفظ النتائج
RESULT_FILE = "results.json"

# ═══════════════════════ استخراج النصوص ═══════════════════════
def extract_text(html: str, base_url: str) -> Dict[str, List[str]]:
    """
    استخرج العناوين والفقرات من الصفحة.
    يمكنك تعديل هذه الدالة لاستخراج أي شيء تريده.
    """
    soup = BeautifulSoup(html, "lxml")

    # مثال: كل العناوين h1 و h2
    titles = [tag.get_text(strip=True) for tag in soup.find_all(["h1", "h2"])]

    # مثال: الفقرات التي يزيد طولها عن 20 حرفاً
    paragraphs = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 20]

    # TODO: أضف استخراج بيانات أخرى (أسعار، جداول، روابط...)
    # links = [a['href'] for a in soup.find_all('a', href=True)]

    return {"titles": titles, "paragraphs": paragraphs}

# ═══════════════════════ استخراج الوسائط ═══════════════════════
def extract_media(html: str, base_url: str) -> Dict[str, List[str]]:
    """
    استخرج روابط الصور والفيديوهات من الصفحة.
    """
    soup = BeautifulSoup(html, "lxml")

    images = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src:
            images.append(urljoin(base_url, src))

    videos = []
    for vid in soup.find_all("video"):
        src = vid.get("src")
        if src:
            videos.append(urljoin(base_url, src))
        for source in vid.find_all("source"):
            if source.get("src"):
                videos.append(urljoin(base_url, source["src"]))

    # حذف التكرارات
    images = list(dict.fromkeys(images))
    videos = list(dict.fromkeys(videos))
    return {"images": images, "videos": videos}

# ═══════════════════════ الكشط الرئيسي ═══════════════════════
async def main():
    save_dir = SAVE_DIR
    media_dir = save_dir / "media"
    save_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)

    # 1. افتح المتصفح وتجاوز أي حماية
    async with AntiDetectionBrowser(headless=HEADLESS) as browser:
        log.info(f"زيارة: {TARGET_URL}")
        html = await browser.visit(TARGET_URL)
        cookies = await browser.get_cookies()
        log.info(f"تم تحميل الصفحة ({len(html)} حرف)، {len(cookies)} كوكيز")

    # 2. استخرج النصوص والوسائط
    text_data = extract_text(html, TARGET_URL)
    media_data = extract_media(html, TARGET_URL)

    # 3. حمّل الوسائط (اختياري)
    downloaded = []
    if DOWNLOAD_MEDIA:
        # أنشئ جلسة curl_cffi باستخدام الكوكيز (تتظاهر بأنها Chrome 120)
        session = create_curl_session(cookies)  # بدون بروكسي
        for i, img_url in enumerate(media_data["images"]):
            try:
                resp = session.get(img_url, timeout=20)
                if resp.status_code == 200 and len(resp.content) > 100:
                    ext = Path(img_url.split("?")[0]).suffix or ".jpg"
                    filepath = media_dir / f"img_{i}{ext}"
                    filepath.write_bytes(resp.content)
                    downloaded.append(str(filepath))
                    log.debug(f"تم تحميل: {filepath.name}")
            except Exception as e:
                log.warning(f"فشل تحميل {img_url}: {e}")

    # 4. احفظ النتائج
    result = {
        "url": TARGET_URL,
        "text": text_data,
        "media_urls": media_data,
        "downloaded_media": downloaded,
    }
    result_path = save_dir / RESULT_FILE
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    log.info(f"تم الحفظ في {result_path}")
    print(json.dumps(result, ensure_ascii=False, indent=2))  # عرض النتيجة

# ═══════════════════════ تشغيل ═══════════════════════
if __name__ == "__main__":
    asyncio.run(main())