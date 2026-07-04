"""
plugins/mangascp.py
===================
Manga-Lionz (Madara-based WordPress) scraper plugin for Sunken Bot API.

يقدّم هذا الـ plugin endpoints لكشط موقع manga-lionz.org (وأي موقع مبني على
قالب Madara نفسه) مع تجاوز Cloudflare بشكل تلقائي عبر ثلاث طبقات:

    1) curl_cffi       — أسرع طبقة (TLS fingerprint لمتصفّح حقيقي).
    2) cloudscraper    — يحلّ تحدّي JS الخفيف في Cloudflare.
    3) browser_engine  — متصفح anti-detection حقيقي (nodriver + curl_cffi)،
                          للـ challenges التفاعلية ("I'm human").

كل الـ imports داخلية — هذا الملف self-contained كما يتطلّب plugin_loader.

Endpoints:
    GET  /mangascp/health            فحص حالة الـ bypass
    POST /mangascp/search            بحث عن مانجا بالاسم
    POST /mangascp/manga             قائمة فصول مانجا معيّنة (slug)
    POST /mangascp/chapter           روابط صور فصل معيّن (خفيف — بدون تحميل)
    POST /mangascp/download          تحميل صفحات فصل كـ base64 (ثقيل)
    POST /mangascp/zip               تحميل صفحات فصل كملف ZIP واحد (مضغوط)

Author: Sunken Bot project
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
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import HTTPException
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Optional dependencies — كلها قد تكون موجودة في requirements الجذر.
# نتجنّب الاستيراد على مستوى الموديول للأشياء الثقيلة (browser_engine/nodriver)
# حتى لا نفشل التحميل لو غير مثبّتة.
# ---------------------------------------------------------------------------
try:
    from curl_cffi import requests as cffi_requests  # type: ignore
    _HAS_CFFI = True
except Exception:  # pragma: no cover
    _HAS_CFFI = False

try:
    import cloudscraper  # type: ignore
    _HAS_CLOUDSCRAPER = True
except Exception:  # pragma: no cover
    _HAS_CLOUDSCRAPER = False

try:
    # نستخدم AntiDetectionBrowser من browser_engine.py (nodriver + curl_cffi)
    # بدل BrowserManager القديم (Playwright خام بلا حماية ضد كشف الأتمتة).
    # نفس مبدأ الاستخدام في نموذج_اوامر.py: متصفح واحد طويل العمر، يُستدعى
    # عبر visit() لكل رابط، ويحل تحديات Cloudflare تلقائياً.
    from browser_engine import AntiDetectionBrowser  # type: ignore
    _HAS_BROWSER_ENGINE = True
except Exception:  # pragma: no cover
    _HAS_BROWSER_ENGINE = False


# ===========================================================================
# Plugin metadata (يقرأها plugin_loader)
# ===========================================================================
DESCRIPTION = "Manga-Lionz / Madara scraper مع تجاوز Cloudflare متعدد الطبقات"
DOCKERFILE_DEPS: List[str] = []  # لا حزم apt إضافية — Chromium يأتي جاهزاً عبر playwright install


# ===========================================================================
# ثوابت الإعدادات
# ===========================================================================
BASE_URL = os.getenv("MANGA_LIONZ_BASE_URL", "https://manga-lionz.org")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# قائمة User-Agents للدوران (تُستخدم عند فشل UA افتراضي).
_FALLBACK_UAS = [
    DEFAULT_USER_AGENT,
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

# مهلات وحدود.
REQUEST_TIMEOUT = 25  # ثانية
DOWNLOAD_TIMEOUT = 40  # ثانية لتحميل الصور
MAX_DOWNLOAD_PAGES = 50  # سقف أمان لعدد الصفحات في طلب download واحد
MAX_CONCURRENT_DOWNLOADS = 6  # عدد الصور المتزامنة
CACHE_TTL_SECONDS = 300  # TTL لنتائج البحث وقوائم الفصول

# أنماط CSS مكتشفة من فحص الموقع (manga-inspector JSON):
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
# نموذج Pydantic للـ request (لا نستخدم BaseModel من plugin_loader — مستقل)
# ===========================================================================
class SearchReq(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(10, ge=1, le=30)


class MangaReq(BaseModel):
    slug: str = Field(..., min_length=1)
    limit: int = Field(500, ge=1, le=2000)  # عدد الفصول كحد أقصى


class ChapterReq(BaseModel):
    url: Optional[str] = None
    slug: Optional[str] = None
    number: Optional[float] = None
    max_pages: int = Field(60, ge=1, le=200)


class DownloadReq(BaseModel):
    url: Optional[str] = None
    slug: Optional[str] = None
    number: Optional[float] = None
    max_pages: int = Field(25, ge=1, le=MAX_DOWNLOAD_PAGES)
    as_zip: bool = False  # نُركه للتوافق — استخدم /mangascp/zip بدلاً


# ===========================================================================
# كاش بسيط في الذاكرة مع TTL
# ===========================================================================
class TTLCache:
    def __init__(self) -> None:
        self._store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        item = self._store.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at < time.time():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: int = CACHE_TTL_SECONDS) -> None:
        self._store[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        self._store.clear()


_cache = TTLCache()


# ===========================================================================
# طبقة تجاوز Cloudflare (waterfall: curl_cffi → cloudscraper → raw httpx)
# ---------------------------------------------------------------------------
# ملاحظة: طبقة browser_engine يمكن تفعيلها لاحقاً عند الحاجة (تُكلّف بطيء).
# ===========================================================================
@dataclass
class FetchResult:
    ok: bool
    status: int
    text: str
    final_url: str
    strategy: str  # "cffi" | "cloudscraper" | "httpx" | "browser_engine" | "cache"


class CloudflareBypass:
    """مدير موحّد لتجاوز Cloudflare مع waterfall تلقائي."""

    CF_CHALLENGE_MARKERS = (
        "cf-mitigated",
        "cf-chl-bypass",
        "checking your browser",
        "just a moment",
        "attention required! | cloudflare",
        "verify you are human",
        "cf_clearance",
    )

    def __init__(self) -> None:
        self._cs_scraper: Optional[Any] = None  # cloudscraper instance
        self._browser: Optional["AntiDetectionBrowser"] = None  # browser_engine — lazy singleton
        self._browser_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._min_interval = 0.4  # أقل فاصل زمني بين طلبين لنفس الموقع

    # ---------- أدوات داخلية ----------
    @staticmethod
    def _looks_like_cf_challenge(html: str, status: int) -> bool:
        if status in (403, 503, 429):
            low = html.lower()
            if any(m in low for m in CloudflareBypass.CF_CHALLENGE_MARKERS):
                return True
            # صفحة فارغة قصيرة في 403/503 غالباً CF
            if len(html) < 4000:
                return True
        return False

    async def _throttle(self) -> None:
        now = time.time()
        wait = self._last_request_at + self._min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = time.time()

    def _pick_ua(self) -> str:
        return random.choice(_FALLBACK_UAS)

    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": self._pick_ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Ch-Ua": '"Chromium";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Upgrade-Insecure-Requests": "1",
        }

    # ---------- الطبقات ----------
    def _try_cffi(self, url: str, method: str = "GET", **kwargs) -> FetchResult:
        if not _HAS_CFFI:
            return FetchResult(False, 0, "", url, "cffi")
        try:
            resp = cffi_requests.request(
                method,
                url,
                impersonate="chrome124",
                headers=self._headers(),
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
                **kwargs,
            )
            text = resp.text or ""
            ok = resp.status_code == 200 and not self._looks_like_cf_challenge(
                text, resp.status_code
            )
            return FetchResult(
                ok=ok,
                status=resp.status_code,
                text=text,
                final_url=str(resp.url),
                strategy="cffi",
            )
        except Exception as exc:  # pragma: no cover
            return FetchResult(False, 0, f"cffi-error: {exc}", url, "cffi")

    def _try_cloudscraper(self, url: str) -> FetchResult:
        if not _HAS_CLOUDSCRAPER:
            return FetchResult(False, 0, "", url, "cloudscraper")
        try:
            if self._cs_scraper is None:
                self._cs_scraper = cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "windows", "mobile": False}
                )
            resp = self._cs_scraper.get(
                url,
                headers=self._headers(),
                timeout=REQUEST_TIMEOUT,
            )
            text = resp.text or ""
            ok = resp.status_code == 200 and not self._looks_like_cf_challenge(
                text, resp.status_code
            )
            return FetchResult(
                ok=ok,
                status=resp.status_code,
                text=text,
                final_url=str(resp.url),
                strategy="cloudscraper",
            )
        except Exception as exc:  # pragma: no cover
            return FetchResult(False, 0, f"cloudscraper-error: {exc}", url, "cloudscraper")

    async def _get_browser(self) -> Optional["AntiDetectionBrowser"]:
        """يشغّل متصفح anti-detection (browser_engine) مرة واحدة فقط ويشاركه بين كل الطلبات."""
        if not _HAS_BROWSER_ENGINE:
            return None
        if self._browser is None:
            async with self._browser_lock:
                if self._browser is None:  # تحقق ثانٍ بعد أخذ القفل
                    b = AntiDetectionBrowser(headless=True)
                    await b.start()
                    self._browser = b
        return self._browser

    async def _try_browser_engine(self, url: str) -> FetchResult:
        """الطبقة الثالثة — متصفح حقيقي عبر browser_engine (nodriver + curl_cffi).

        هذه هي الطبقة الوحيدة القادرة على حل تحديات Cloudflare التفاعلية
        ("Just a moment..." / "Verify you are human") لأنها تُنفّذ الجافاسكربت
        فعلياً بدل تقليد بصمة TLS فقط (على عكس curl_cffi وcloudscraper)، مع
        حماية إضافية ضد كشف الأتمتة (nodriver) مقارنةً بـ Playwright الخام.
        AntiDetectionBrowser.visit() يتكفّل داخلياً بانتظار حل التحدي والتفاعل
        البشري المحاكى، فلا حاجة هنا لإعادة تنفيذ حلقة الانتظار يدوياً.
        """
        if not _HAS_BROWSER_ENGINE:
            return FetchResult(False, 0, "", url, "browser_engine")
        try:
            browser = await self._get_browser()
            if browser is None:
                return FetchResult(False, 0, "", url, "browser_engine")

            html = await browser.visit(url, timeout=REQUEST_TIMEOUT)
            final_url = getattr(browser.page, "url", None) or url
            status = 200 if html else 0
            ok = bool(html) and not self._looks_like_cf_challenge(
                html, status if status else 403
            )
            return FetchResult(
                ok=ok,
                status=status,
                text=html or "",
                final_url=final_url,
                strategy="browser_engine",
            )
        except Exception as exc:  # pragma: no cover
            return FetchResult(False, 0, f"browser_engine-error: {exc}", url, "browser_engine")

    async def _try_httpx(self, url: str) -> FetchResult:
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
                headers=self._headers(),
            ) as client:
                resp = await client.get(url)
                text = resp.text or ""
                ok = resp.status_code == 200 and not self._looks_like_cf_challenge(
                    text, resp.status_code
                )
                return FetchResult(
                    ok=ok,
                    status=resp.status_code,
                    text=text,
                    final_url=str(resp.url),
                    strategy="httpx",
                )
        except Exception as exc:
            return FetchResult(False, 0, f"httpx-error: {exc}", url, "httpx")

    # ---------- الـ API الموحّدة ----------
    async def get(self, url: str, use_cache: bool = True) -> FetchResult:
        """يجلب URL مع تجاوز CF. waterfall: cffi → cloudscraper → browser_engine → httpx."""
        if use_cache:
            cached = _cache.get(f"get::{url}")
            if cached is not None:
                return FetchResult(True, 200, cached, url, "cache")

        await self._throttle()

        last_result: Optional[FetchResult] = None

        # 1) curl_cffi (الأسرع والأقوى عادةً)
        result = await asyncio.to_thread(self._try_cffi, url)
        last_result = result
        if result.ok:
            if use_cache:
                _cache.set(f"get::{url}", result.text, ttl=120)
            return result

        # 2) cloudscraper (حلّ JS challenges الخفيفة)
        if _HAS_CLOUDSCRAPER:
            result2 = await asyncio.to_thread(self._try_cloudscraper, url)
            last_result = result2
            if result2.ok:
                if use_cache:
                    _cache.set(f"get::{url}", result2.text, ttl=120)
                return result2

        # 3) browser_engine (متصفح anti-detection حقيقي — يحلّ تحديات "Just a
        #    moment" التفاعلية التي لا يقدر cffi ولا cloudscraper على حلها لأنها
        #    تتطلب تنفيذ JS فعلي)
        if _HAS_BROWSER_ENGINE:
            result3 = await self._try_browser_engine(url)
            last_result = result3
            if result3.ok:
                if use_cache:
                    _cache.set(f"get::{url}", result3.text, ttl=120)
                return result3

        # 4) httpx عادي — ملاذ أخير بلا أي تجاوز حماية، قد ينجح فقط لو
        #    الـ challenge كان مؤقتاً أو غاب عن هذا الطلب بالذات.
        result4 = await self._try_httpx(url)
        last_result = result4
        if result4.ok:
            if use_cache:
                _cache.set(f"get::{url}", result4.text, ttl=120)
            return result4

        # فشل كامل — نُرجع آخر محاولة فعلياً حصلت (وليس دائماً محاولة cffi
        # الأولى) حتى يعكس حقل "strategy" الطبقة التي فشلت فعلياً في الأخير.
        return last_result if last_result is not None else result

    async def post(self, url: str, data: Dict[str, str]) -> FetchResult:
        """POST مع تجاوز CF (مُحسَّن لطلبات WordPress AJAX)."""
        await self._throttle()
        if _HAS_CFFI:
            try:
                resp = await asyncio.to_thread(
                    cffi_requests.post,
                    url,
                    data=data,
                    impersonate="chrome124",
                    headers={
                        **self._headers(),
                        "X-Requested-With": "XMLHttpRequest",
                        "Origin": BASE_URL,
                        "Referer": BASE_URL + "/",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                text = resp.text or ""
                ok = resp.status_code == 200
                return FetchResult(ok, resp.status_code, text, str(resp.url), "cffi")
            except Exception as exc:
                return FetchResult(False, 0, f"cffi-post-error: {exc}", url, "cffi")
        # fallback
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers=self._headers()) as client:
            resp = await client.post(url, data=data)
            return FetchResult(resp.status_code == 200, resp.status_code, resp.text, str(resp.url), "httpx")

    async def download_bytes(self, url: str) -> Tuple[Optional[bytes], str]:
        """يُنزّل بايتات (صور). لا يمرّ عبر CF عادةً — s3leo CDN مكشوف."""
        # هذه الصور من s3leo — لا تحتاج CF bypass.
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content:
                    return resp.content, resp.headers.get("content-type", "image/jpeg")
            except Exception:
                pass
        return None, ""


_bypass = CloudflareBypass()


# ===========================================================================
# منطق الكشط (Madara-based WordPress theme)
# ===========================================================================
class MangaScraper:
    """منطق عالي المستوى لموقع manga-lionz.org."""

    @staticmethod
    def _abs(base: str, url: str) -> str:
        if not url:
            return ""
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return urljoin(base, url)
        return url

    @staticmethod
    def _parse_search_ajax(html: str) -> List[Dict[str, str]]:
        """يُحلّل ردّ /wp-admin/admin-ajax.php (Madara search)."""
        import json

        html = (html or "").strip()
        if not html:
            return []
        # الردّ JSON لكن أحياناً مُغلّف بـ <body> أو HTML entities
        try:
            data = json.loads(html)
        except Exception:
            # محاولة تنظيف بسيطة
            cleaned = re.sub(r"^[^{]*", "", html)
            cleaned = re.sub(r"[^}]*$", "", cleaned)
            try:
                data = json.loads(cleaned)
            except Exception:
                return []

        results: List[Dict[str, str]] = []
        # البنية المعتادة: {"success": true, "data": [{"title": "...", "url": "..."}]}
        if isinstance(data, dict):
            items = data.get("data") or data.get("mangas") or []
            if isinstance(items, list):
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    title = it.get("title") or it.get("name") or ""
                    url = it.get("url") or it.get("link") or ""
                    if title and url:
                        results.append({"title": title.strip(), "url": url.strip()})
        elif isinstance(data, list):
            for it in data:
                if isinstance(it, dict):
                    title = it.get("title") or ""
                    url = it.get("url") or ""
                    if title and url:
                        results.append({"title": title.strip(), "url": url.strip()})
        return results

    @staticmethod
    def _parse_chapter_list(html: str, base: str) -> List[Dict[str, Any]]:
        """يستخرج قائمة الفصول من صفحة مانجا."""
        soup = BeautifulSoup(html or "", "lxml")
        chapters: List[Dict[str, Any]] = []
        seen: set[str] = set()

        for sel in CHAPTER_LIST_SELECTORS:
            for a in soup.select(sel):
                href = a.get("href") or ""
                if not href or href in seen:
                    continue
                # تأكّد أن الرابط يبدو كفصل (يحتوي على رقم في النهاية)
                m = re.search(r"/([^/]+)/?$", href.rstrip("/"))
                if not m:
                    continue
                tail = m.group(1)
                # فلترة روابط غير الفصول (مثل "chapter-list")
                if not re.search(r"\d", tail):
                    continue
                seen.add(href)

                full_url = MangaScraper._abs(base, href)
                # استخرج رقم الفصل من النص أو الـ URL
                text = a.get_text(strip=True)
                num_match = re.search(r"(\d+(?:\.\d+)?)", text) or re.search(
                    r"(\d+(?:\.\d+)?)", tail
                )
                number = float(num_match.group(1)) if num_match else None

                # تاريخ (اختياري)
                date = ""
                parent = a.parent
                if parent:
                    for cls in ("chapter-release-date", "c-new", "time"):
                        el = parent.select_one(f".{cls}, time, span.{cls}")
                        if el:
                            date = el.get_text(strip=True)
                            break

                chapters.append(
                    {
                        "title": text or f"الفصل {int(number) if number else tail}",
                        "number": number,
                        "url": full_url,
                        "date": date,
                    }
                )

        # ترتيب تنازلي حسب رقم الفصل
        chapters.sort(
            key=lambda c: (c["number"] is None, -(c["number"] or 0)),
        )
        return chapters

    @staticmethod
    def _parse_chapter_pages(html: str, base: str) -> Tuple[str, List[Dict[str, Any]]]:
        """يستخرج روابط الصور + عنوان الفصل."""
        soup = BeautifulSoup(html or "", "lxml")

        # العنوان
        title = ""
        for sel in ("h1", ".chapter-title", ".post-title h1", "title"):
            el = soup.select_one(sel)
            if el:
                t = el.get_text(strip=True)
                if t and len(t) < 200:
                    title = t
                    break

        pages: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for sel in CHAPTER_IMG_SELECTORS:
            for img in soup.select(sel):
                src = (
                    img.get("data-src")
                    or img.get("data-lazy-src")
                    or img.get("data-original")
                    or img.get("src")
                    or ""
                )
                if not src:
                    continue
                # تجاهل الـ placeholders وصور الـ UI
                low_src = src.lower()
                if any(
                    skip in low_src
                    for skip in (
                        "logo",
                        "loading",
                        "placeholder",
                        "data:image",
                        "avatar",
                        "ads",
                        "banner",
                    )
                ):
                    continue
                full_url = MangaScraper._abs(base, src)
                if full_url in seen:
                    continue
                seen.add(full_url)
                # استخرج الامتداد
                ext = "jpg"
                m = re.search(r"\.(jpg|jpeg|png|webp|gif)(?:\?|$)", low_src)
                if m:
                    ext = m.group(1)
                    if ext == "jpeg":
                        ext = "jpg"
                pages.append(
                    {
                        "index": len(pages) + 1,
                        "url": full_url,
                        "ext": ext,
                        "width": img.get("width"),
                        "height": img.get("height"),
                    }
                )

        return title, pages

    # ---------- الـ API ----------
    async def search(self, query: str, limit: int = 10) -> List[Dict[str, str]]:
        cache_key = f"search::{query.lower()}::{limit}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

        ajax_url = f"{BASE_URL}/wp-admin/admin-ajax.php"
        result = await _bypass.post(
            ajax_url,
            data={"action": "wp-manga-search-manga", "title": query},
        )

        items: List[Dict[str, str]] = []
        if result.ok and result.text:
            items = self._parse_search_ajax(result.text)
            items = items[:limit]

        if not items:
            # fallback: استخدم صفحة البحث HTML مباشرة
            search_url = f"{BASE_URL}/?s={query.replace(' ', '+')}&post_type=wp-manga"
            r2 = await _bypass.get(search_url)
            if r2.ok:
                soup = BeautifulSoup(r2.text, "lxml")
                for a in soup.select("a[href*='/manga/']"):
                    href = a.get("href", "")
                    if "/manga/" in href and href.rstrip("/").count("/") >= 3:
                        title = a.get_text(strip=True) or a.get("title", "")
                        if title and len(title) > 1:
                            items.append({"title": title, "url": href})

        # إزالة التكرار
        unique: List[Dict[str, str]] = []
        seen_urls: set[str] = set()
        for it in items:
            if it["url"] in seen_urls:
                continue
            seen_urls.add(it["url"])
            unique.append(it)
        items = unique[:limit]

        _cache.set(cache_key, items, ttl=300)
        return items

    async def get_manga(self, slug: str, limit: int = 500) -> Dict[str, Any]:
        slug = slug.strip().strip("/")
        url = f"{BASE_URL}/manga/{slug}/"
        cache_key = f"manga::{slug}::{limit}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

        result = await _bypass.get(url)
        if not result.ok:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "fetch_failed",
                    "url": url,
                    "status": result.status,
                    "strategy": result.strategy,
                    "snippet": (result.text or "")[:300],
                },
            )

        soup = BeautifulSoup(result.text, "lxml")
        title = ""
        for sel in (".post-title h1", ".manga-title", "h1"):
            el = soup.select_one(sel)
            if el:
                title = el.get_text(strip=True)
                if title:
                    break

        cover = ""
        og = soup.select_one('meta[property="og:image"]')
        if og and og.get("content"):
            cover = og["content"]

        chapters = self._parse_chapter_list(result.text, BASE_URL)
        if limit:
            chapters = chapters[:limit]

        out = {
            "slug": slug,
            "url": url,
            "title": title,
            "cover": cover,
            "chapters": chapters,
            "total_chapters": len(chapters),
            "fetch_strategy": result.strategy,
        }
        _cache.set(cache_key, out, ttl=300)
        return out

    async def get_chapter(
        self,
        url: Optional[str] = None,
        slug: Optional[str] = None,
        number: Optional[float] = None,
        max_pages: int = 60,
    ) -> Dict[str, Any]:
        if not url:
            if not slug or number is None:
                raise HTTPException(
                    status_code=400,
                    detail="either 'url' or both 'slug' and 'number' are required",
                )
            # رقم الفصل الصحيح كعدد صحيح (Madara لا تستخدم floats)
            num_str = str(int(number)) if float(number).is_integer() else str(number)
            url = f"{BASE_URL}/manga/{slug.strip('/')}/{num_str}/"

        cache_key = f"chapter::{url}::{max_pages}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

        result = await _bypass.get(url)
        if not result.ok:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "fetch_failed",
                    "url": url,
                    "status": result.status,
                    "strategy": result.strategy,
                    "snippet": (result.text or "")[:300],
                },
            )

        title, pages = self._parse_chapter_pages(result.text, BASE_URL)
        if max_pages:
            pages = pages[:max_pages]

        out = {
            "url": url,
            "title": title,
            "pages": pages,
            "total_pages": len(pages),
            "fetch_strategy": result.strategy,
        }
        _cache.set(cache_key, out, ttl=600)
        return out

    async def download_chapter(
        self,
        url: Optional[str] = None,
        slug: Optional[str] = None,
        number: Optional[float] = None,
        max_pages: int = MAX_DOWNLOAD_PAGES,
    ) -> Dict[str, Any]:
        """يُنزّل صفحات فصل ويعيدها كـ base64."""
        info = await self.get_chapter(
            url=url, slug=slug, number=number, max_pages=max_pages
        )
        pages = info["pages"]
        if not pages:
            raise HTTPException(
                status_code=404,
                detail={"error": "no_pages_found", "url": info["url"]},
            )

        sem = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

        async def fetch_one(page: Dict[str, Any]) -> Dict[str, Any]:
            async with sem:
                data, ctype = await _bypass.download_bytes(page["url"])
                if not data:
                    return {**page, "ok": False, "error": "download_failed"}
                return {
                    "index": page["index"],
                    "ext": page["ext"],
                    "content_type": ctype or f"image/{page['ext']}",
                    "size": len(data),
                    "b64": base64.b64encode(data).decode("ascii"),
                    "ok": True,
                }

        results = await asyncio.gather(
            *(fetch_one(p) for p in pages), return_exceptions=False
        )

        ok_count = sum(1 for r in results if r.get("ok"))
        return {
            "url": info["url"],
            "title": info["title"],
            "fetch_strategy": info["fetch_strategy"],
            "requested_pages": len(pages),
            "downloaded_pages": ok_count,
            "images": results,
        }

    async def download_chapter_zip(
        self,
        url: Optional[str] = None,
        slug: Optional[str] = None,
        number: Optional[float] = None,
        max_pages: int = MAX_DOWNLOAD_PAGES,
    ) -> Dict[str, Any]:
        """يُنزّل صفحات فصل ويُرجعها كملف ZIP واحد base64."""
        info = await self.get_chapter(
            url=url, slug=slug, number=number, max_pages=max_pages
        )
        pages = info["pages"]
        if not pages:
            raise HTTPException(
                status_code=404, detail={"error": "no_pages_found", "url": info["url"]}
            )

        sem = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

        async def fetch_bytes(page: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[bytes]]:
            async with sem:
                data, _ = await _bypass.download_bytes(page["url"])
                return page, data

        fetched = await asyncio.gather(*(fetch_bytes(p) for p in pages))

        # بناء ZIP في الذاكرة
        buf = io.BytesIO()
        downloaded = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for page, data in fetched:
                if not data:
                    continue
                fname = f"{page['index']:03d}.{page['ext']}"
                zf.writestr(fname, data)
                downloaded += 1

        zip_bytes = buf.getvalue()
        safe_title = re.sub(r"[^\w\-\u0600-\u06FF]+", "_", info["title"])[:80] or "chapter"
        slug_part = ""
        if not url and slug:
            slug_part = f"{slug}_"
        num_part = ""
        if number is not None:
            num_part = f"{int(number) if float(number).is_integer() else number}_"
        elif url:
            m = re.search(r"/manga/[^/]+/([^/]+)/?", url)
            if m:
                num_part = f"{m.group(1)}_"
        filename = f"{slug_part}{num_part}{safe_title}.zip".strip("_")

        return {
            "url": info["url"],
            "title": info["title"],
            "requested_pages": len(pages),
            "downloaded_pages": downloaded,
            "filename": filename,
            "size": len(zip_bytes),
            "zip_b64": base64.b64encode(zip_bytes).decode("ascii"),
            "fetch_strategy": info["fetch_strategy"],
        }


_scraper = MangaScraper()


# ===========================================================================
# endpoints
# ===========================================================================
def register(app):  # noqa: C901 — accepts FastAPI app
    @app.get("/mangascp/health")
    async def health() -> Dict[str, Any]:
        """فحص حالة طبقة تجاوز CF + قابلية الوصول للموقع."""
        url = f"{BASE_URL}/"
        result = await _bypass.get(url, use_cache=False)
        return {
            "ok": result.ok,
            "base_url": BASE_URL,
            "status": result.status,
            "strategy": result.strategy,
            "title_hint": _quick_title(result.text) if result.ok else "",
            "layers": {
                "curl_cffi": _HAS_CFFI,
                "cloudscraper": _HAS_CLOUDSCRAPER,
                "browser_engine": _HAS_BROWSER_ENGINE,
            },
        }

    @app.post("/mangascp/search")
    async def search(req: SearchReq) -> Dict[str, Any]:
        items = await _scraper.search(req.query, limit=req.limit)
        return {
            "query": req.query,
            "count": len(items),
            "results": items,
        }

    @app.post("/mangascp/manga")
    async def manga_info(req: MangaReq) -> Dict[str, Any]:
        return await _scraper.get_manga(req.slug, limit=req.limit)

    @app.post("/mangascp/chapter")
    async def chapter(req: ChapterReq) -> Dict[str, Any]:
        return await _scraper.get_chapter(
            url=req.url,
            slug=req.slug,
            number=req.number,
            max_pages=req.max_pages,
        )

    @app.post("/mangascp/download")
    async def download(req: DownloadReq) -> Dict[str, Any]:
        return await _scraper.download_chapter(
            url=req.url,
            slug=req.slug,
            number=req.number,
            max_pages=req.max_pages,
        )

    @app.post("/mangascp/zip")
    async def download_zip(req: DownloadReq) -> Dict[str, Any]:
        return await _scraper.download_chapter_zip(
            url=req.url,
            slug=req.slug,
            number=req.number,
            max_pages=req.max_pages,
        )


def _quick_title(html: str) -> str:
    if not html:
        return ""
    m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    return (m.group(1).strip() if m else "")[:120]