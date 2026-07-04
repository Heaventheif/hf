"""
plugins/mangascp.py – نسخة مبسطة مع دعم البروكسيات المجانية
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import random
import re
import time
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from fastapi import HTTPException
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# استيرادات اختيارية
# ---------------------------------------------------------------------------
try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except Exception:
    _HAS_CFFI = False

try:
    import cloudscraper
    _HAS_CLOUDSCRAPER = True
except Exception:
    _HAS_CLOUDSCRAPER = False

try:
    from browser_engine import AntiDetectionBrowser
    _HAS_BROWSER_ENGINE = True
except Exception:
    _HAS_BROWSER_ENGINE = False

# ===========================================================================
# بيانات الـ Plugin
# ===========================================================================
DESCRIPTION = "Manga-Lionz / Madara scraper مع تجاوز Cloudflare متعدد الطبقات + بروكسيات"
DOCKERFILE_DEPS: List[str] = []

# ===========================================================================
# قائمة بروكسيات مجانية (مختصرة للاختبار – يمكنك إضافة المزيد)
# ===========================================================================
FREE_PROXIES = [
    "8.210.17.35:8080",
    "93.113.63.11:3128",
    "172.235.198.182:1080",
    "163.172.53.142:80",
    "62.60.149.161:3128",
    "219.249.37.107:8382",
    "185.230.190.195:3128",
    "159.223.87.50:443",
    "91.188.213.143:1080",
    "191.252.111.160:7000",
    "70.35.196.194:8087",
    "31.57.178.195:8080",
    "45.95.232.35:3128",
    "103.129.127.244:8088",
    "118.70.13.38:41857",
    "132.243.234.171:9443",
    "144.202.14.153:50000",
    "45.168.244.16:8080",
    "94.102.6.4:3310",
    "51.178.253.98:80",
]

# ===========================================================================
# إعدادات
# ===========================================================================
BASE_URL = os.getenv("MANGA_LIONZ_BASE_URL", "https://manga-lionz.org")
REQUEST_TIMEOUT = 25
DOWNLOAD_TIMEOUT = 40
CACHE_TTL_SECONDS = 300

CHAPTER_IMG_SELECTORS = [
    ".reading-content img",
    ".page-break img",
    ".reading-content .page-break img",
    "div.text-left img",
    "img.wp-manga-chapter-img",
]
CHAPTER_LIST_SELECTORS = [
    ".listing-chapters_wrap a",
    "ul.chapter-list li a",
    ".chapter-list a",
    "li.wp-manga-chapter a",
    ".chapters a",
]

# ===========================================================================
# النماذج
# ===========================================================================
class SearchReq(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(10, ge=1, le=30)

class MangaReq(BaseModel):
    slug: str = Field(..., min_length=1)
    limit: int = Field(500, ge=1, le=2000)

class ChapterReq(BaseModel):
    url: Optional[str] = None
    slug: Optional[str] = None
    number: Optional[float] = None
    max_pages: int = Field(60, ge=1, le=200)

class DownloadReq(BaseModel):
    url: Optional[str] = None
    slug: Optional[str] = None
    number: Optional[float] = None
    max_pages: int = Field(25, ge=1, le=50)

# ===========================================================================
# كاش بسيط
# ===========================================================================
class TTLCache:
    def __init__(self):
        self._store: Dict[str, Tuple[float, Any]] = {}
    def get(self, key):
        item = self._store.get(key)
        if not item: return None
        if time.time() > item[0]:
            self._store.pop(key, None)
            return None
        return item[1]
    def set(self, key, value, ttl=CACHE_TTL_SECONDS):
        self._store[key] = (time.time() + ttl, value)

_cache = TTLCache()

# ===========================================================================
# أدوات مساعدة
# ===========================================================================
def _random_ua():
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    ]
    return random.choice(uas)

def _headers():
    return {
        "User-Agent": _random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }

def _looks_like_cf(html, status):
    if status in (403, 503, 429):
        low = html.lower()
        if any(x in low for x in ("just a moment", "cloudflare", "cf-mitigated", "verify you are human")):
            return True
        if len(html) < 4000:
            return True
    return False

# ===========================================================================
# مدير الطلبات مع البروكسيات
# ===========================================================================
class CloudflareBypass:
    def __init__(self):
        self._browser = None
        self._browser_lock = asyncio.Lock()
        self._last_request = 0.0
        self._min_interval = 0.4

    async def _throttle(self):
        now = time.time()
        wait = self._last_request + self._min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = time.time()

    def _get_proxy_dict(self):
        """ترجع بروكسي عشوائي كـ dict بصيغة curl_cffi."""
        proxy_str = random.choice(FREE_PROXIES)
        return {"http": f"http://{proxy_str}", "https": f"https://{proxy_str}"}

    # ---- curl_cffi مع بروكسي ----
    def _try_cffi(self, url, proxy=None):
        if not _HAS_CFFI:
            return None
        try:
            resp = cffi_requests.get(
                url,
                impersonate="chrome124",
                headers=_headers(),
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
                proxies=proxy,
            )
            text = resp.text or ""
            ok = resp.status_code == 200 and not _looks_like_cf(text, resp.status_code)
            return {"ok": ok, "status": resp.status_code, "text": text, "strategy": "cffi"}
        except Exception:
            return None

    # ---- cloudscraper ----
    def _try_cloudscraper(self, url):
        if not _HAS_CLOUDSCRAPER:
            return None
        try:
            scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows"})
            resp = scraper.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
            text = resp.text or ""
            ok = resp.status_code == 200 and not _looks_like_cf(text, resp.status_code)
            return {"ok": ok, "status": resp.status_code, "text": text, "strategy": "cloudscraper"}
        except Exception:
            return None

    # ---- browser_engine (مع أو بدون بروكسي) ----
    async def _try_browser(self, url, proxy=None):
        if not _HAS_BROWSER_ENGINE:
            return None
        try:
            if proxy:
                # ننشئ متصفح جديد مع البروكسي
                browser = AntiDetectionBrowser(headless=True, proxy=proxy)
                await browser.start()
            else:
                if self._browser is None:
                    async with self._browser_lock:
                        if self._browser is None:
                            proxy_env = os.getenv("MANGA_PROXY") or None
                            self._browser = AntiDetectionBrowser(headless=True, proxy=proxy_env)
                            await self._browser.start()
                browser = self._browser

            html = await browser.visit(url, timeout=REQUEST_TIMEOUT)
            ok = bool(html) and not _looks_like_cf(html, 200)
            return {"ok": ok, "status": 200, "text": html or "", "strategy": "browser_engine"}
        except Exception:
            return None

    # ---- httpx عادي ----
    async def _try_httpx(self, url):
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=_headers()) as client:
                resp = await client.get(url)
                text = resp.text or ""
                ok = resp.status_code == 200 and not _looks_like_cf(text, resp.status_code)
                return {"ok": ok, "status": resp.status_code, "text": text, "strategy": "httpx"}
        except Exception:
            return None

    # ---- الطلب الرئيسي ----
    async def get(self, url, use_cache=True):
        if use_cache:
            cached = _cache.get(url)
            if cached:
                return cached

        await self._throttle()
        result = None

        # 1) جرب curl_cffi مع 3 بروكسيات مختلفة
        for _ in range(3):
            proxy = self._get_proxy_dict()
            r = self._try_cffi(url, proxy)
            if r and r["ok"]:
                result = r
                break
            await asyncio.sleep(0.3)

        # 2) جرب cloudscraper
        if not result:
            r = self._try_cloudscraper(url)
            if r and r["ok"]:
                result = r

        # 3) جرب browser_engine مع بروكسي (محاولة واحدة)
        if not result:
            proxy_str = random.choice(FREE_PROXIES)
            r = await self._try_browser(url, proxy=f"http://{proxy_str}")
            if r and r["ok"]:
                result = r

        # 4) جرب browser_engine بدون بروكسي
        if not result:
            r = await self._try_browser(url, proxy=None)
            if r and r["ok"]:
                result = r

        # 5) أخيراً httpx
        if not result:
            r = await self._try_httpx(url)
            if r and r["ok"]:
                result = r

        if result and result["ok"]:
            _cache.set(url, result["text"])
            return result

        # فشل كامل – نعيد خطأ يحمل تفاصيل المحاولات
        return {"ok": False, "status": 403, "text": "فشل جميع المحاولات", "strategy": "failed"}

    async def post(self, url, data):
        # مشابه لـ GET ولكن نستخدم POST مع بروكسي
        await self._throttle()
        proxy = self._get_proxy_dict()
        try:
            if _HAS_CFFI:
                resp = await asyncio.to_thread(
                    cffi_requests.post,
                    url,
                    data=data,
                    impersonate="chrome124",
                    headers={**_headers(), "X-Requested-With": "XMLHttpRequest"},
                    timeout=REQUEST_TIMEOUT,
                    proxies=proxy,
                )
                text = resp.text or ""
                ok = resp.status_code == 200
                return {"ok": ok, "status": resp.status_code, "text": text, "strategy": "cffi_post"}
        except Exception:
            pass
        # fallback
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers=_headers()) as client:
            resp = await client.post(url, data=data)
            return {"ok": resp.status_code == 200, "status": resp.status_code, "text": resp.text, "strategy": "httpx_post"}

    async def download_bytes(self, url):
        try:
            async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content:
                    return resp.content, resp.headers.get("content-type", "image/jpeg")
        except Exception:
            pass
        return None, ""

_bypass = CloudflareBypass()


# ===========================================================================
# منطق الكشط (نفس السابق)
# ===========================================================================
class MangaScraper:
    @staticmethod
    def _abs(base, url):
        if not url: return ""
        if url.startswith("//"): return "https:" + url
        if url.startswith("/"): return urljoin(base, url)
        return url

    @staticmethod
    def _parse_search_ajax(html):
        import json
        html = (html or "").strip()
        if not html: return []
        try:
            data = json.loads(html)
        except Exception:
            return []
        results = []
        if isinstance(data, dict):
            items = data.get("data") or data.get("mangas") or []
            for it in items:
                if isinstance(it, dict) and it.get("title") and it.get("url"):
                    results.append({"title": it["title"], "url": it["url"]})
        return results

    @staticmethod
    def _parse_chapter_list(html, base):
        soup = BeautifulSoup(html or "", "lxml")
        chapters = []
        seen = set()
        for sel in CHAPTER_LIST_SELECTORS:
            for a in soup.select(sel):
                href = a.get("href")
                if not href or href in seen: continue
                seen.add(href)
                full_url = MangaScraper._abs(base, href)
                text = a.get_text(strip=True)
                num_match = re.search(r"(\d+(?:\.\d+)?)", text)
                number = float(num_match.group(1)) if num_match else None
                chapters.append({"title": text, "number": number, "url": full_url})
        chapters.sort(key=lambda c: c["number"] or 0, reverse=True)
        return chapters

    @staticmethod
    def _parse_chapter_pages(html, base):
        soup = BeautifulSoup(html or "", "lxml")
        title = ""
        for sel in ("h1", ".chapter-title", ".post-title h1", "title"):
            el = soup.select_one(sel)
            if el:
                t = el.get_text(strip=True)
                if t: title = t; break

        pages = []
        seen = set()
        for sel in CHAPTER_IMG_SELECTORS:
            for img in soup.select(sel):
                src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
                if not src: continue
                full_url = MangaScraper._abs(base, src)
                if full_url in seen: continue
                seen.add(full_url)
                ext = re.search(r"\.(jpg|jpeg|png|webp|gif)", full_url.lower())
                ext = ext.group(1) if ext else "jpg"
                if ext == "jpeg": ext = "jpg"
                pages.append({"index": len(pages)+1, "url": full_url, "ext": ext})
        return title, pages

    async def search(self, query, limit=10):
        cache_key = f"search_{query}"
        cached = _cache.get(cache_key)
        if cached: return cached

        result = await _bypass.post(f"{BASE_URL}/wp-admin/admin-ajax.php",
                                    {"action": "wp-manga-search-manga", "title": query})
        items = []
        if result and result["ok"]:
            items = self._parse_search_ajax(result["text"])
        if not items:
            # fallback
            r2 = await _bypass.get(f"{BASE_URL}/?s={query.replace(' ', '+')}&post_type=wp-manga")
            if r2 and r2["ok"]:
                soup = BeautifulSoup(r2["text"], "lxml")
                for a in soup.select("a[href*='/manga/']"):
                    href = a.get("href")
                    title = a.get_text(strip=True)
                    if href and title:
                        items.append({"title": title, "url": href})
        items = items[:limit]
        _cache.set(cache_key, items)
        return items

    async def get_manga(self, slug, limit=500):
        url = f"{BASE_URL}/manga/{slug.strip('/')}/"
        cache_key = f"manga_{slug}"
        cached = _cache.get(cache_key)
        if cached: return cached

        result = await _bypass.get(url)
        if not result or not result["ok"]:
            raise HTTPException(502, detail=f"فشل جلب المانجا: {result}")

        soup = BeautifulSoup(result["text"], "lxml")
        title = soup.select_one("h1")
        title = title.get_text(strip=True) if title else slug
        chapters = self._parse_chapter_list(result["text"], BASE_URL)
        if limit: chapters = chapters[:limit]
        out = {"slug": slug, "title": title, "chapters": chapters, "total_chapters": len(chapters)}
        _cache.set(cache_key, out)
        return out

    async def get_chapter(self, url=None, slug=None, number=None, max_pages=60):
        if not url:
            if not slug or number is None:
                raise HTTPException(400, "يجب توفير url أو (slug+number)")
            num = str(int(number)) if float(number).is_integer() else str(number)
            url = f"{BASE_URL}/manga/{slug.strip('/')}/{num}/"

        cache_key = f"chapter_{url}"
        cached = _cache.get(cache_key)
        if cached: return cached

        result = await _bypass.get(url)
        if not result or not result["ok"]:
            raise HTTPException(502, detail=f"فشل جلب الفصل: {result}")

        title, pages = self._parse_chapter_pages(result["text"], BASE_URL)
        if max_pages: pages = pages[:max_pages]
        out = {"url": url, "title": title, "pages": pages, "total_pages": len(pages)}
        _cache.set(cache_key, out)
        return out

    async def download_chapter(self, url=None, slug=None, number=None, max_pages=25):
        info = await self.get_chapter(url, slug, number, max_pages)
        pages = info["pages"]
        if not pages:
            raise HTTPException(404, "لا توجد صور في هذا الفصل")

        sem = asyncio.Semaphore(6)
        async def fetch_one(page):
            async with sem:
                data, ctype = await _bypass.download_bytes(page["url"])
                if not data:
                    return {**page, "ok": False}
                return {
                    "index": page["index"],
                    "ext": page["ext"],
                    "b64": base64.b64encode(data).decode("ascii"),
                    "ok": True
                }

        results = await asyncio.gather(*(fetch_one(p) for p in pages))
        ok_count = sum(1 for r in results if r.get("ok"))
        return {
            "url": info["url"],
            "title": info["title"],
            "requested_pages": len(pages),
            "downloaded_pages": ok_count,
            "images": results,
        }

_scraper = MangaScraper()

# ===========================================================================
# endpoints
# ===========================================================================
def register(app):
    @app.get("/mangascp/health")
    async def health():
        return {"status": "ok", "proxies": len(FREE_PROXIES)}

    @app.post("/mangascp/search")
    async def search(req: SearchReq):
        items = await _scraper.search(req.query, req.limit)
        return {"query": req.query, "count": len(items), "results": items}

    @app.post("/mangascp/manga")
    async def manga_info(req: MangaReq):
        return await _scraper.get_manga(req.slug, req.limit)

    @app.post("/mangascp/chapter")
    async def chapter(req: ChapterReq):
        return await _scraper.get_chapter(req.url, req.slug, req.number, req.max_pages)

    @app.post("/mangascp/download")
    async def download(req: DownloadReq):
        return await _scraper.download_chapter(req.url, req.slug, req.number, req.max_pages)