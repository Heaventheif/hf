"""Light HTTP client (fast mode).

Uses ``curl_cffi`` to impersonate a real Chrome TLS fingerprint (Pinterest
flags most plain-``httpx`` / ``requests`` traffic) and ``cloudscraper`` as
a fallback for pages with Cloudflare-style challenges.

The client is async-friendly and runs inside the same event loop as the
FastAPI app.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

import cloudscraper
from curl_cffi.requests import AsyncSession

from .config import settings

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-ch-ua": '"Not)A;Brand";v="99", "Chromium";v="127", "Google Chrome";v="127"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}


class HttpClient:
    """Async HTTP client with anti-bot fingerprint + retry."""

    def __init__(self) -> None:
        self._session: Optional[AsyncSession] = None
        self._scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "linux", "mobile": False}
        )
        self._last_request_at = 0.0

    async def start(self) -> None:
        self._session = AsyncSession(
            impersonate="chrome127",
            headers=DEFAULT_HEADERS,
            timeout=settings.timeout_s,
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def get(self, url: str) -> str:
        """GET ``url`` with retries and TLS fingerprint impersonation."""
        await self._respect_rate_limit()

        attempt = 0
        last_err: Optional[Exception] = None
        while attempt <= settings.max_retries:
            try:
                assert self._session is not None
                resp = await self._session.get(
                    url,
                    headers={"user-agent": self._pick_ua()},
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.text
            except Exception as exc:  # network, 429, 5xx
                last_err = exc
                wait = min(30, (2 ** attempt) + random.random())
                log.warning(
                    "GET %s attempt %d failed: %s — sleeping %.1fs",
                    url, attempt + 1, exc, wait,
                )
                await asyncio.sleep(wait)
                attempt += 1
        raise RuntimeError(f"GET {url} failed after {settings.max_retries} retries: {last_err}")

    async def _respect_rate_limit(self) -> None:
        now = asyncio.get_event_loop().time()
        elapsed_ms = (now - self._last_request_at) * 1000
        target = random.randint(settings.min_delay_ms, settings.max_delay_ms)
        if elapsed_ms < target:
            await asyncio.sleep((target - elapsed_ms) / 1000)
        self._last_request_at = asyncio.get_event_loop().time()

    @staticmethod
    def _pick_ua() -> str:
        return random.choice(settings.user_agents)
