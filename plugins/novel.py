"""
plugins/novel.py
endpoint: POST /novel
يجلب فصول الروايات من freewebnovel.com
"""

import re
import time
import asyncio
import logging
import httpx
from typing import Optional
from fastapi import Request
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup

logger = logging.getLogger("novel")
DESCRIPTION = "جلب فصول الروايات من freewebnovel.com"

# ─── Cache ─────────────────────────────────────────────────────
_cache: dict = {}
CACHE_TTL = 3600

def _cache_get(key: str):
    item = _cache.get(key)
    if not item:
        return None
    if time.time() > item["expires"]:
        del _cache[key]
        return None
    return item["value"]

def _cache_set(key: str, value):
    if len(_cache) >= 300:
        oldest = next(iter(_cache))
        del _cache[oldest]
    _cache[key] = {"value": value, "expires": time.time() + CACHE_TTL}

# ─── User Agents ──────────────────────────────────────────────
import random
UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]
def rua(): return random.choice(UAS)

HEADERS = lambda: {
    "User-Agent": rua(),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

# ─── slugify ──────────────────────────────────────────────────
def slugify(name: str) -> str:
    return re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", name.lower().replace("'", "")))

# ─── فلترة النص ───────────────────────────────────────────────
FILTER_WORDS = [
    "advertisement", "report chapter", "next chapter", "prev chapter",
    "table of contents", "access denied", "just a moment", "cloudflare",
    "enable javascript", "read more at",
    "cookie", "privacy", "terms of service", "subscribe",
]
STOLEN = [
    re.compile(r"stol(en|e)\s+(content|chapter)", re.I),
    re.compile(r"if\s+you.re\s+reading\s+this\s+on", re.I),
    re.compile(r"unauthorized\s+(use|reproduction)", re.I),
]

def is_filtered(t: str) -> bool:
    lo = t.lower()
    if len(t) < 15:
        return True
    return any(w in lo for w in FILTER_WORDS) or any(p.search(t) for p in STOLEN)

def clean(t: str) -> str:
    return re.sub(r"\s{2,}", " ", re.sub(r"\.{4,}", "...", t.replace("\u00a0", " "))).strip()

# ─── استخراج محتوى محسّن ──────────────────────────────────────
def extract(html: str, selectors: list[str]) -> Optional[list[str]]:
    soup = BeautifulSoup(html, "lxml")

    # إزالة العناصر المزعجة
    for tag in soup.select("script,style,ins,.ads,noscript,nav,header,footer,.advertisement"):
        tag.decompose()

    container = None
    # 1. محاولة المحددات المحددة
    for sel in selectors:
        try:
            el = soup.select_one(sel)
            if el and len(el.get_text()) > 200:
                container = el
                break
        except Exception:
            continue

    # 2. إذا لم نجد، استخدم .m-read
    if not container:
        container = soup.select_one(".m-read")
        if container and len(container.get_text()) > 200:
            logger.info("استخدم .m-read كحاوية")
        else:
            container = None

    # 3. أخيراً، استخدم body
    if not container:
        logger.info("استخدم body كحاوية أخيرة")
        container = soup.body

    if not container:
        return None

    # استخراج الفقرات
    paras = [clean(p.get_text()) for p in container.find_all("p")]
    paras = [p for p in paras if len(p) > 15 and not is_filtered(p)]

    # إذا كانت الفقرات قليلة، جرب تقسيم النص على أسطر
    if len(paras) < 3:
        raw = [clean(l) for l in container.get_text(separator="\n").split("\n")]
        paras = [p for p in raw if len(p) > 15 and not is_filtered(p)]

    # إذا لم توجد فقرات كافية، قسّم النص إلى جمل
    if len(paras) < 2:
        text = container.get_text()
        sentences = re.split(r'[.!?]\s+', text)
        paras = [clean(s) for s in sentences if len(s) > 20 and not is_filtered(s)]

    return paras if len(paras) >= 2 else None

def extract_title(html: str, selectors: list[str]) -> str:
    soup = BeautifulSoup(html, "lxml")
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            t = re.split(r"[–\-|]", el.get_text())[0].strip()
            if len(t) > 2:
                return t
    # محاولة من .tit أو .chapter
    tit = soup.select_one(".tit")
    if tit:
        t = tit.get_text().strip()
        if len(t) > 2:
            return t
    title_tag = soup.find("title")
    if title_tag:
        t = title_tag.get_text().strip()
        if len(t) > 2:
            return t.split(" - ")[0]
    return ""

# ─── طلب HTTP عادي ────────────────────────────────────────────
async def fetch_page_http(url: str, timeout: int = 30, retries: int = 2) -> Optional[str]:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for attempt in range(retries + 1):
            try:
                resp = await client.get(url, headers=HEADERS())
                if resp.status_code == 200 and len(resp.text) > 500:
                    lower = resp.text[:3000].lower()
                    if "just a moment" in lower or "cloudflare" in lower:
                        logger.warning(f"Cloudflare على {url}")
                        break
                    return resp.text
            except httpx.TimeoutException:
                if attempt == retries:
                    logger.warning(f"Timeout بعد {retries} محاولات: {url}")
            except Exception as e:
                if attempt == retries:
                    logger.warning(f"فشل جلب {url}: {e}")
            await asyncio.sleep(1)
        return None

# ─── طلب باستخدام Playwright ──────────────────────────────────
async def fetch_page_playwright(url: str, wait_sel: str = None, timeout: int = 45000) -> Optional[str]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright غير مثبت")
        return None

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        try:
            page = await browser.new_page(user_agent=rua())
            await page.goto(url, wait_until="networkidle", timeout=timeout)
            if wait_sel:
                try:
                    await page.wait_for_selector(wait_sel, timeout=15000)
                except Exception:
                    pass
            await asyncio.sleep(2)
            html = await page.content()
            return html if len(html) > 500 else None
        finally:
            await browser.close()

# ─── الموقع (مع المسار الصحيح) ─────────────────────────────────
SITE = {
    "name": "Freewebnovel",
    # ✅ الرابط الصحيح مع /novel/
    "build_url": lambda slug, ch: f"https://freewebnovel.com/novel/{slug}/chapter-{ch}",
    "selectors": [
        "#chapter-content",
        ".chapter-content",
        "#content",
        ".content",
        "#reading-content",
        ".reading-content",
        "#article",
        "div#article",
        "div[class*='chapter']",
        ".txt",
        "div.txt",
        "article",
        "main",
        ".m-read",
        "body",
    ],
    "title_sel": ["h1.tit", ".tit", "h1", ".chapter", "title"],
    "wait_selector": ".m-read, #chapter-content, .chapter-content, article, main, body",
}

async def fetch_chapter(novel_name: str, chapter_num: int) -> dict:
    key = f"freewebnovel:{novel_name.lower()}:{chapter_num}"
    cached = _cache_get(key)
    if cached:
        return cached

    slug = slugify(novel_name)
    if not slug:
        raise ValueError("اسم الرواية غير صالح")

    url = SITE["build_url"](slug, chapter_num)
    html = None

    # 1. HTTP عادي
    logger.info(f"[HTTP] جلب {url}")
    html = await fetch_page_http(url)

    # 2. HTTP مع .html
    if not html:
        url_html = f"{url}.html"
        logger.info(f"[HTTP] جرب .html: {url_html}")
        html = await fetch_page_http(url_html)

    # 3. Playwright
    if not html:
        logger.info(f"[Playwright] جلب {url}")
        html = await fetch_page_playwright(url, wait_sel=SITE.get("wait_selector"))

    if not html:
        raise ValueError(f"فشل جلب الصفحة بكل الطرق: {url}")

    paragraphs = extract(html, SITE["selectors"])
    if not paragraphs:
        sample = html[:1000].replace("\n", " ")
        logger.warning(f"عينة HTML: {sample}")
        raise ValueError("لم يُعثر على محتوى (تحقق من بنية الصفحة)")

    title = extract_title(html, SITE["title_sel"]) or novel_name

    result = {
        "title": title,
        "chapter": chapter_num,
        "paragraphs": paragraphs,
        "site": SITE["name"],
        "url": url,
        "word_count": sum(len(p.split()) for p in paragraphs),
    }
    _cache_set(key, result)
    return result

# ─── endpoints ──────────────────────────────────────────────────
def register(app):

    @app.post("/novel")
    async def get_chapter(request: Request):
        try:
            body = await request.json()
            novel_name = body.get("novel", "").strip()
            chapter_num = body.get("chapter")

            if not novel_name:
                return JSONResponse({"error": "novel مطلوب"}, status_code=400)
            if not chapter_num or int(str(chapter_num)) < 1:
                return JSONResponse({"error": "chapter موجب"}, status_code=400)
            chapter_num = int(chapter_num)

            result = await fetch_chapter(novel_name, chapter_num)
            return JSONResponse(result)

        except ValueError as e:
            logger.warning(f"خطأ: {e}")
            return JSONResponse({"error": str(e)}, status_code=404)
        except Exception as e:
            logger.exception("خطأ غير متوقع")
            return JSONResponse({"error": str(e)[:200]}, status_code=500)

    @app.get("/novel/sites")
    async def list_sites():
        return {"sites": [SITE["name"]]}

    @app.delete("/novel/cache")
    async def clear_cache():
        count = len(_cache)
        _cache.clear()
        return {"cleared": count}