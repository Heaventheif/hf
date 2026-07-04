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
# قائمة البروكسيات المجانية (من free-proxy-list.net بتاريخ 2026-07-04)
# ===========================================================================
FREE_PROXIES = [
    "8.210.17.35:8080", "93.113.63.11:3128", "172.235.198.182:1080",
    "163.172.53.142:80", "62.60.149.161:3128", "219.249.37.107:8382",
    "185.230.190.195:3128", "159.223.87.50:443", "91.188.213.143:1080",
    "191.252.111.160:7000", "70.35.196.194:8087", "31.57.178.195:8080",
    "45.95.232.35:3128", "103.129.127.244:8088", "118.70.13.38:41857",
    "132.243.234.171:9443", "144.202.14.153:50000", "45.168.244.16:8080",
    "94.102.6.4:3310", "51.178.253.98:80", "149.129.255.179:4002",
    "200.227.89.50:3128", "80.252.137.246:1194", "23.81.87.202:8118",
    "132.243.246.97:8443", "103.69.96.15:8888", "89.207.72.188:8083",
    "104.239.13.215:6844", "145.223.56.18:7070", "166.88.195.12:5644",
    "146.103.56.42:5590", "217.69.121.164:5829", "23.27.91.108:6187",
    "206.206.71.200:5840", "107.175.135.137:6578", "20.78.26.206:8561",
    "209.97.149.157:80", "31.57.41.203:5779", "31.59.33.107:6683",
    "82.23.203.46:5354", "84.46.204.249:6552", "84.46.204.180:6483",
    "104.143.224.252:6113", "104.143.226.161:5764", "84.46.204.216:6519",
    "62.133.62.3:1082", "62.133.62.207:1081", "62.133.62.249:1082",
    "77.110.113.236:8080", "80.151.57.81:8080", "47.238.203.170:50000",
    "157.180.84.115:443", "181.204.39.202:26312", "175.139.233.79:80",
    "86.62.2.25:3128", "51.210.5.144:3129", "203.162.13.26:6868",
    "194.26.192.168:8080", "47.79.254.162:8888", "172.237.73.24:80",
    "12.50.107.219:80", "109.236.88.82:80", "45.225.207.248:999",
    "152.32.132.190:7890", "143.42.66.91:80", "117.236.124.166:3128",
    "37.187.74.125:80", "217.60.33.157:1080", "190.58.248.86:80",
    "12.50.107.220:80", "91.107.182.124:82", "85.105.98.6:5314",
    "176.12.65.24:443", "113.160.132.26:8080", "94.182.225.248:3128",
    "41.59.90.170:80", "202.28.194.139:31280", "213.33.126.130:80",
    "43.133.169.103:7890", "197.221.234.149:80", "197.221.234.253:80",
    "183.110.216.159:8090", "183.110.216.128:8090", "176.99.134.183:8090",
    "27.34.242.98:80", "34.44.49.215:80", "185.200.188.234:10001",
    "42.2.4.163:1080", "138.91.159.185:80", "41.220.22.7:80",
    "46.249.100.124:80", "39.109.113.97:4090", "103.94.52.70:3128",
    "41.59.90.171:80", "82.114.228.67:1080", "47.236.86.147:443",
    "45.157.140.12:1080", "133.18.234.13:80", "85.97.109.179:1953",
    "202.133.88.173:80", "41.220.16.215:80", "103.30.211.34:80",
    "185.181.209.34:8080", "13.114.160.78:80", "159.195.69.220:8888",
    "49.51.228.35:81", "41.220.16.211:80", "34.43.46.91:80",
    "197.221.237.248:80", "41.220.16.218:80", "197.221.249.196:80",
    "46.47.197.210:3128", "52.34.243.150:8080", "219.93.101.60:80",
    "219.93.101.62:80", "5.45.126.128:8080", "162.255.110.24:8080",
    "177.52.221.100:999", "181.204.81.178:999", "154.6.83.142:6613",
    "45.61.124.199:6528", "198.12.112.83:5094", "107.172.221.179:6134",
    "89.249.198.214:6300", "136.0.189.206:6933", "145.223.58.206:6475",
    "161.123.33.67:6090", "192.210.191.211:6197", "185.216.106.232:6309",
    "45.115.195.217:6195", "86.38.236.33:6317", "136.0.126.170:5931",
    "86.38.26.117:6282", "98.159.38.218:6518", "206.206.124.6:6587",
    "146.103.55.236:6288", "198.46.246.117:6741", "104.143.224.59:5920",
    "86.38.154.179:5822", "45.61.125.71:6082", "31.58.18.122:6391",
    "104.232.209.1:5959", "31.59.33.87:6663", "206.206.118.112:6350",
    "195.158.8.123:3128", "8.221.141.88:31433", "206.189.144.164:10808",
    "104.128.228.69:8118", "173.212.245.136:8888", "185.121.13.73:3128",
    "54.38.139.182:3128", "81.177.160.200:80", "78.189.49.28:1953",
    "88.247.30.179:1953", "78.187.90.223:5314", "45.93.22.42:8080",
    "36.232.129.211:8080", "138.2.64.185:8118", "62.133.62.231:1081",
    "62.133.62.184:1082", "150.241.116.167:443", "83.222.7.205:3128",
    "178.128.95.176:8080", "185.60.136.237:3129", "197.221.240.176:80",
    "85.198.100.232:3128", "34.96.238.40:8080", "103.43.191.71:8888",
    "41.184.92.220:80", "197.221.240.247:80", "41.220.16.208:80",
    "182.53.202.208:8080", "109.120.184.202:1080", "159.195.49.27:8888",
    "94.198.218.123:3128", "79.111.13.155:50625", "188.127.224.164:2080",
    "203.162.13.222:6868", "45.32.8.165:6688", "144.91.121.61:3129",
    "62.133.62.17:1081", "62.133.62.12:1081", "151.243.153.157:8118",
    "8.215.25.3:2080", "45.95.233.237:1082", "159.65.245.255:80",
    "174.138.119.88:80", "103.65.237.92:5678", "91.107.163.9:82",
    "85.105.163.43:1953", "95.9.81.181:1953", "217.154.155.115:8080",
    "47.245.117.43:80", "69.2.15.238:8111", "64.64.127.251:6204",
    "67.227.37.64:5606", "142.147.132.44:6239", "136.0.186.128:6489",
    "136.0.127.88:5797", "81.90.29.194:10808", "103.107.136.70:8081",
    "188.129.8.242:81", "114.6.27.84:8520", "104.154.186.48:80",
    "174.137.134.182:2999", "103.167.61.162:3128", "97.74.87.226:80",
    "45.188.125.49:999", "80.82.55.71:80", "103.156.224.66:8080",
    "143.255.85.180:999", "157.66.16.52:8080", "38.158.83.241:999",
    "103.167.116.141:8087", "38.75.82.217:999", "103.230.244.4:8090",
    "103.167.23.139:8080", "197.164.101.11:1976", "181.79.95.65:999",
    "193.29.224.20:3128", "65.109.87.121:18080", "222.228.194.131:8080",
    "197.221.240.246:80", "41.220.16.213:80", "47.90.167.27:8123",
    "94.158.49.82:3128", "85.104.108.167:1953", "78.189.92.15:1953",
    "31.223.7.155:1953", "88.248.105.150:1953", "85.104.111.214:1953",
    "65.108.203.35:28080", "123.100.136.11:8080", "103.204.211.48:32255",
    "123.58.219.150:7890", "181.16.201.37:80", "46.8.112.212:3128",
    "8.215.3.250:8119", "135.125.154.99:8899", "185.204.168.189:443",
    "140.245.238.56:53", "194.59.204.87:9080", "208.82.61.64:3128",
    "181.129.183.19:53281", "14.199.167.175:3128", "185.120.217.42:8090",
    "175.158.40.224:1616", "168.126.169.198:808", "194.180.189.246:3128",
    "45.144.30.59:808", "131.222.251.36:8080", "103.172.120.102:8097",
    "38.211.76.177:999", "41.128.72.141:1981", "210.87.74.181:8080",
    "62.133.60.5:3128", "212.34.146.118:3128", "207.246.68.214:3129",
    "8.213.156.191:6666", "8.211.194.78:1081", "47.89.159.212:8123",
    "8.220.141.8:9090", "138.124.106.230:443", "72.56.238.99:9090",
    "71.198.208.169:443", "47.83.168.191:4000", "12.50.107.222:80",
    "41.184.92.219:80", "85.98.40.129:1953", "62.248.56.111:1953",
    "88.248.115.49:1953", "88.225.230.45:5314", "54.38.138.60:3128",
    "199.189.255.230:1080", "12.50.107.217:80", "75.84.71.14:80",
    "212.231.191.23:80", "147.78.0.81:9443", "34.87.80.221:30000",
    "207.180.254.198:8080", "197.221.249.199:80", "147.45.76.207:3128",
    "197.221.240.178:80", "219.65.73.81:80", "219.93.101.63:80",
    "142.147.240.217:6739", "145.223.46.203:5753", "173.245.88.5:5308",
    "104.168.118.46:6002", "104.233.26.162:6000", "23.27.75.168:6248",
    "84.46.204.24:6327", "136.0.207.173:6750", "20.27.11.248:8561",
    "86.38.26.191:6356", "185.226.204.228:5781", "45.43.70.128:6415",
    "86.38.154.152:5795"
]


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
# طبقة تجاوز Cloudflare (waterfall: curl_cffi → cloudscraper → browser_engine → httpx)
# ---------------------------------------------------------------------------
# ملاحظة: تم إضافة دعم البروكسيات المجانية مع تناوب عشوائي ومحاولات متعددة.
# ===========================================================================
@dataclass
class FetchResult:
    ok: bool
    status: int
    text: str
    final_url: str
    strategy: str  # "cffi" | "cloudscraper" | "httpx" | "browser_engine" | "cache"
    attempts: Optional[List[Dict[str, Any]]] = None


class CloudflareBypass:
    """مدير موحّد لتجاوز Cloudflare مع waterfall تلقائي ودعم البروكسيات."""

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
        self._proxy_blacklist: set = set()  # بروكسيات فشلت مؤقتاً

    # ---------- أدوات داخلية ----------
    @staticmethod
    def _looks_like_cf_challenge(html: str, status: int) -> bool:
        if status in (403, 503, 429):
            low = html.lower()
            if any(m in low for m in CloudflareBypass.CF_CHALLENGE_MARKERS):
                return True
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

    # ---------- إدارة البروكسيات ----------
    def _get_random_proxy(self) -> Optional[Dict[str, str]]:
        """ترجع بروكسي عشوائي من القائمة (مع تجاهل المحظورين مؤقتاً)."""
        available = [p for p in FREE_PROXIES if p not in self._proxy_blacklist]
        if not available:
            return None
        proxy_str = random.choice(available)
        return {
            "http": f"http://{proxy_str}",
            "https": f"https://{proxy_str}"
        }

    def _mark_proxy_failed(self, proxy_str: str) -> None:
        """يُضيف البروكسي إلى القائمة السوداء لمدة 5 دقائق."""
        self._proxy_blacklist.add(proxy_str)
        # تنظيف القائمة السوداء بعد 5 دقائق (لأن بعض البروكسيات قد تعود للعمل)
        asyncio.create_task(self._clear_blacklist_after(proxy_str, 300))

    async def _clear_blacklist_after(self, proxy_str: str, delay: int) -> None:
        await asyncio.sleep(delay)
        self._proxy_blacklist.discard(proxy_str)

    # ---------- الطبقات ----------
    def _try_cffi(self, url: str, method: str = "GET", proxy: Optional[Dict] = None, **kwargs) -> FetchResult:
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
                proxies=proxy,
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
        except Exception as exc:
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
        except Exception as exc:
            return FetchResult(False, 0, f"cloudscraper-error: {exc}", url, "cloudscraper")

    async def _get_browser(self, proxy: Optional[str] = None) -> Optional["AntiDetectionBrowser"]:
        """يشغّل متصفح anti-detection (browser_engine) مع بروكسي اختياري."""
        if not _HAS_BROWSER_ENGINE:
            return None
        # إذا كان هناك بروكسي محدد، ننشئ متصفحاً جديداً بهذا البروكسي
        if proxy:
            b = AntiDetectionBrowser(headless=True, proxy=proxy)
            await b.start()
            return b
        # وإلا نستخدم المتصفح المخزن (بدون بروكسي)
        if self._browser is None:
            async with self._browser_lock:
                if self._browser is None:
                    proxy_env = os.getenv("MANGA_PROXY") or None
                    b = AntiDetectionBrowser(headless=True, proxy=proxy_env)
                    await b.start()
                    self._browser = b
        return self._browser

    async def _try_browser_engine(self, url: str, proxy: Optional[str] = None) -> FetchResult:
        if not _HAS_BROWSER_ENGINE:
            return FetchResult(False, 0, "", url, "browser_engine")
        try:
            browser = await self._get_browser(proxy)
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
        except Exception as exc:
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

        attempts: List[Dict[str, Any]] = []

        def record(r: FetchResult) -> FetchResult:
            attempts.append(
                {
                    "strategy": r.strategy,
                    "status": r.status,
                    "ok": r.ok,
                    "snippet": (r.text or "")[:200],
                }
            )
            return r

        # ── 1) جرب curl_cffi مع بروكسيات متعددة (5 محاولات) ──
        for _ in range(5):
            proxy = self._get_random_proxy()
            if proxy:
                result = record(self._try_cffi(url, proxy=proxy))
                if result.ok:
                    if use_cache:
                        _cache.set(f"get::{url}", result.text, ttl=120)
                    return result
                # إذا فشل بسبب تحدي CF، ضع البروكسي في القائمة السوداء
                if result.status in (403, 503) and result.text and "just a moment" in result.text.lower():
                    proxy_str = proxy.get("http", "").replace("http://", "")
                    if proxy_str:
                        self._mark_proxy_failed(proxy_str)
            await asyncio.sleep(0.5)

        # ── 2) cloudscraper (بدون بروكسي) ──
        if _HAS_CLOUDSCRAPER:
            result2 = record(await asyncio.to_thread(self._try_cloudscraper, url))
            if result2.ok:
                if use_cache:
                    _cache.set(f"get::{url}", result2.text, ttl=120)
                return result2

        # ── 3) browser_engine مع بروكسيات متعددة (3 محاولات) ──
        for _ in range(3):
            proxy_str = self._get_random_proxy()
            proxy = proxy_str.get("http", "").replace("http://", "") if proxy_str else None
            if proxy:
                result3 = record(await self._try_browser_engine(url, proxy=proxy))
                if result3.ok:
                    if use_cache:
                        _cache.set(f"get::{url}", result3.text, ttl=120)
                    return result3
                # إذا فشل، ضع البروكسي في القائمة السوداء
                self._mark_proxy_failed(proxy)
            await asyncio.sleep(1)

        # ── 4) browser_engine بدون بروكسي (آخر محاولة) ──
        if _HAS_BROWSER_ENGINE:
            result4 = record(await self._try_browser_engine(url, proxy=None))
            if result4.ok:
                if use_cache:
                    _cache.set(f"get::{url}", result4.text, ttl=120)
                return result4

        # ── 5) httpx عادي (ملاذ أخير) ──
        result5 = record(await self._try_httpx(url))
        if result5.ok:
            if use_cache:
                _cache.set(f"get::{url}", result5.text, ttl=120)
            return result5

        # فشل كامل
        final = result5
        final.attempts = attempts
        return final

    async def post(self, url: str, data: Dict[str, str]) -> FetchResult:
        """POST مع تجاوز CF (مُحسَّن لطلبات WordPress AJAX)."""
        await self._throttle()
        # جرّب مع بروكسي عشوائي
        proxy = self._get_random_proxy()
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
                    proxies=proxy,
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
        import json
        html = (html or "").strip()
        if not html:
            return []
        try:
            data = json.loads(html)
        except Exception:
            cleaned = re.sub(r"^[^{]*", "", html)
            cleaned = re.sub(r"[^}]*$", "", cleaned)
            try:
                data = json.loads(cleaned)
            except Exception:
                return []

        results: List[Dict[str, str]] = []
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
        soup = BeautifulSoup(html or "", "lxml")
        chapters: List[Dict[str, Any]] = []
        seen: set[str] = set()

        for sel in CHAPTER_LIST_SELECTORS:
            for a in soup.select(sel):
                href = a.get("href") or ""
                if not href or href in seen:
                    continue
                m = re.search(r"/([^/]+)/?$", href.rstrip("/"))
                if not m:
                    continue
                tail = m.group(1)
                if not re.search(r"\d", tail):
                    continue
                seen.add(href)

                full_url = MangaScraper._abs(base, href)
                text = a.get_text(strip=True)
                num_match = re.search(r"(\d+(?:\.\d+)?)", text) or re.search(
                    r"(\d+(?:\.\d+)?)", tail
                )
                number = float(num_match.group(1)) if num_match else None

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

        chapters.sort(
            key=lambda c: (c["number"] is None, -(c["number"] or 0)),
        )
        return chapters

    @staticmethod
    def _parse_chapter_pages(html: str, base: str) -> Tuple[str, List[Dict[str, Any]]]:
        soup = BeautifulSoup(html or "", "lxml")

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
                    "attempts": result.attempts,
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
                    "attempts": result.attempts,
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
def register(app):
    @app.get("/mangascp/health")
    async def health() -> Dict[str, Any]:
        url = f"{BASE_URL}/"
        result = await _bypass.get(url, use_cache=False)
        return {
            "ok": result.ok,
            "base_url": BASE_URL,
            "status": result.status,
            "strategy": result.strategy,
            "title_hint": _quick_title(result.text) if result.ok else "",
            "layers": {
                "curl_cffi_imported": _HAS_CFFI,
                "cloudscraper_imported": _HAS_CLOUDSCRAPER,
                "browser_engine_imported": _HAS_BROWSER_ENGINE,
            },
            "attempts": result.attempts or [
                {"strategy": result.strategy, "status": result.status, "ok": result.ok}
            ],
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