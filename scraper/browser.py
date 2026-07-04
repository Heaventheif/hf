"""Playwright browser manager (full mode).

Pins a single Chromium instance for the lifetime of the scraper.  Each
``page()`` invocation creates a new ``BrowserContext`` so cookies / local
storage / cache don't leak between scrapes (except the persistent
pinterest.com login cookies, which are re-applied to every new context).

Anti-bot hardening:
  * ``--disable-blink-features=AutomationControlled``
  * Realistic viewport (1280x900) + locale
  * Randomized User-Agent
  * Extended stealth init-script (navigator.webdriver/plugins/languages,
    chrome runtime object, permissions.query patch)
  * pinterest.com login cookies injected into every context (see cookies.py)
  * Optional ``--proxy-server`` via ``SCRAPER_PROXY`` env var
"""
from __future__ import annotations

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, List, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Response,
    async_playwright,
)

from .config import settings
from .cookies import load_pinterest_cookies

log = logging.getLogger(__name__)

GRAPHQL_HOSTS = ("/_/graphql/", "BoardsFeedResource", "PinResource")

# نفس علامات تحدي Cloudflare/الأنظمة المشابهة المستخدمة في browser_engine.py —
# مدمجة هنا حتى BrowserManager يقدر يكتشف وينتظر حل التحدي بنفس المنطق.
CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "ddos protection",
    "verify you are human",
)

# سكربت stealth موسّع — يغطي أكثر من علم automation واحد (مو بس webdriver).
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = window.chrome || { runtime: {} };
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
    window.navigator.permissions.query = (params) => (
        params && params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(params)
    );
}
"""


class BrowserManager:
    """Async context around a long-lived Playwright Chromium instance."""

    def __init__(self, headless: Optional[bool] = None) -> None:
        self.headless = headless if headless is not None else settings.headless
        self._pw = None
        self._browser: Optional[Browser] = None
        self._lock = asyncio.Lock()
        # تُقرأ مرة واحدة فقط عند start() — إعادة قراءة الملف بكل context
        # مضيعة، والكوكيز غالباً ثابتة طول عمر البروسس.
        self._pinterest_cookies: List[Dict] = []

    async def start(self) -> None:
        if self._browser is not None:
            return
        self._pw = await async_playwright().start()
        launch_kwargs: Dict = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
            ],
        }
        proxy = _read_proxy_from_env()
        if proxy:
            launch_kwargs["proxy"] = proxy
        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        self._pinterest_cookies = load_pinterest_cookies(settings.cookies_file)
        log.info(
            "Playwright Chromium started (headless=%s, pinterest_cookies=%d)",
            self.headless,
            len(self._pinterest_cookies),
        )

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    @asynccontextmanager
    async def context(self) -> AsyncIterator[BrowserContext]:
        """Yield a fresh isolated context, pre-authenticated with the
        pinterest.com cookies loaded at start()."""
        async with self._lock:
            if self._browser is None:
                await self.start()
            assert self._browser is not None
            ctx = await self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=random.choice(settings.user_agents),
                locale="en-US",
                timezone_id="America/New_York",
                ignore_https_errors=True,
            )
            await ctx.add_init_script(STEALTH_INIT_SCRIPT)
            if self._pinterest_cookies:
                await ctx.add_cookies(self._pinterest_cookies)
        try:
            yield ctx
        finally:
            await ctx.close()

    @asynccontextmanager
    async def page(self) -> AsyncIterator[Page]:
        async with self.context() as ctx:
            page = await ctx.new_page()
            try:
                yield page
            finally:
                await page.close()


async def goto_with_retry(
    page: Page,
    url: str,
    timeout: int = 30_000,
    retries: int = 2,
) -> None:
    """يتنقّل لـ URL مع إعادة محاولة عند timeout — بدل ما نستسلم من أول
    محاولة (سبب رسالة "Page.goto: Timeout 30000ms exceeded" اللي كانت
    تظهر للمستخدم مباشرة بدون أي إعادة محاولة).

    يجرّب ``domcontentloaded`` أولاً (أسرع)، ولو فشل يجرّب ``load`` كملاذ
    أخير قبل رفع الاستثناء الحقيقي.
    """
    last_exc: Optional[Exception] = None
    wait_strategies = ["domcontentloaded", "load"]
    for attempt in range(retries + 1):
        strategy = wait_strategies[min(attempt, len(wait_strategies) - 1)]
        try:
            await page.goto(url, wait_until=strategy, timeout=timeout)
            return
        except Exception as exc:  # timeout أو خطأ شبكة
            last_exc = exc
            log.warning(
                "goto(%s) فشل بمحاولة %d/%d (%s): %s",
                url, attempt + 1, retries + 1, strategy, exc,
            )
            await asyncio.sleep(1.5 + random.random())
    raise last_exc  # type: ignore[misc]


async def wait_for_challenge_to_clear(page: Page, max_wait_s: float = 20.0) -> bool:
    """نفس منطق ``browser_engine._solve_challenges`` — ينتظر حتى يختفي أي
    من علامات تحدي Cloudflare من عنوان/محتوى الصفحة. يرجّع True لو الصفحة
    صافية (أو ما كان فيها تحدي أصلاً)، False لو استمر التحدي بعد المهلة."""
    deadline = asyncio.get_event_loop().time() + max_wait_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            title = (await page.title()) or ""
            body = await page.evaluate("document.body ? document.body.innerText : ''")
        except Exception:
            return True  # الصفحة تغيّرت أثناء الفحص — غالباً معناها انتقلنا فعلاً
        combined = f"{title} {body}".lower()
        if not any(marker in combined for marker in CHALLENGE_MARKERS):
            return True
        log.info("[browser] تحدي محتمل مكتشف، بالانتظار...")
        await asyncio.sleep(2.0)
    return False


def _read_proxy_from_env() -> Optional[Dict[str, str]]:
    proxy = None  # SCRAPER_PROXY placeholder
    if not proxy:
        return None
    return {"server": proxy}


def is_graphql_response(resp: Response) -> bool:
    url = resp.url
    return any(token in url for token in GRAPHQL_HOSTS)


async def safe_json(resp: Response) -> Optional[dict]:
    try:
        return await resp.json()
    except Exception:
        return None

