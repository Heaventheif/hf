"""Playwright browser manager (full mode).

Pins a single Chromium instance for the lifetime of the scraper.  Each
``page()`` invocation creates a new ``BrowserContext`` so cookies / local
storage / cache don't leak between scrapes.

Anti-bot hardening:
  * ``--disable-blink-features=AutomationControlled``
  * Realistic viewport (1280x900) + locale
  * Randomized User-Agent
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

log = logging.getLogger(__name__)

GRAPHQL_HOSTS = ("/_/graphql/", "BoardsFeedResource", "PinResource")


class BrowserManager:
    """Async context around a long-lived Playwright Chromium instance."""

    def __init__(self, headless: Optional[bool] = None) -> None:
        self.headless = headless if headless is not None else settings.headless
        self._pw = None
        self._browser: Optional[Browser] = None
        self._lock = asyncio.Lock()

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
        log.info("Playwright Chromium started (headless=%s)", self.headless)

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    @asynccontextmanager
    async def context(self) -> AsyncIterator[BrowserContext]:
        """Yield a fresh isolated context."""
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
            # Strip the webdriver flag from the navigator.
            await ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )
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
