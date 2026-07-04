"""High-level :class:`PinterestScraper` — the only class users need.

Two execution modes:

* ``fast``  – ``curl_cffi`` over HTTPS + BeautifulSoup parsing.  No JS,
  very fast, works on the SSR parts of Pinterest.
* ``full``  – Playwright Chromium.  Scrolls the page, captures GraphQL
  responses, waits for late-loading content.
* ``auto``  – fast first, fall back to full when fast returns < 5 pins
  (a strong signal that the page needs JS rendering).

Async context manager:

    async with PinterestScraper() as s:
        result = await s.scrape_page("https://www.pinterest.com/")
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .browser import (
    BrowserManager,
    goto_with_retry,
    is_graphql_response,
    safe_json,
    wait_for_challenge_to_clear,
)
from .config import settings
from .exporters import flatten_pin, to_csv, to_json
from .http import HttpClient
from .models import Board, Pin, Profile, ScrapeResult
from .parsers import PinParser, ProfileParser

log = logging.getLogger(__name__)

BASE_URL = "https://www.pinterest.com"
FAST_MIN_PINS_BEFORE_FALLBACK = 5


# ---------------------------------------------------------------------------
# Pin extraction helpers for the full / browser mode
# ---------------------------------------------------------------------------

EXTRACT_JS = r"""
() => {
    const cards = document.querySelectorAll('.ADXRXN');
    const out = [];
    const seen = new Set();
    for (const c of cards) {
        const a = c.querySelector('a[href*="/pin/"]');
        if (!a) continue;
        const m = (a.getAttribute('href') || '').match(/\/pin\/(\d+)/);
        if (!m) continue;
        const id = m[1];
        if (seen.has(id)) continue;
        seen.add(id);
        const img = c.querySelector('img');
        const h = c.querySelector('h1, h2, h3, [class*="title"]');
        out.push({
            id,
            url: a.href,
            title: h ? h.textContent.trim() : null,
            image_src: img ? img.getAttribute('src') : null,
            image_srcset: img ? img.getAttribute('srcset') : null
        });
    }
    return out;
}
"""

EXTRACT_PROFILE_JS = r"""
(username) => {
    const boards = [];
    const seen = new Set();
    const skip = new Set(['pins', 'saved', '_saved', 'today']);
    const re = new RegExp(`/${username}/([^/?#]+)/?$`);
    document.querySelectorAll(`a[href*="/${username}/"]`).forEach(a => {
        const href = a.getAttribute('href') || '';
        const m = href.match(re);
        if (!m) return;
        const slug = m[1];
        if (skip.has(slug) || seen.has(slug)) return;
        seen.add(slug);
        boards.push({
            slug,
            url: a.href,
            title: (a.textContent || '').trim().slice(0, 120) || null
        });
    });
    const cards = document.querySelectorAll('.ADXRXN');
    const pins = [];
    const seenPins = new Set();
    for (const c of cards) {
        const a = c.querySelector('a[href*="/pin/"]');
        if (!a) continue;
        const m = (a.getAttribute('href') || '').match(/\/pin\/(\d+)/);
        if (!m) continue;
        const id = m[1];
        if (seenPins.has(id)) continue;
        seenPins.add(id);
        const img = c.querySelector('img');
        const h = c.querySelector('h1, h2, h3, [class*="title"]');
        pins.push({
            id,
            url: a.href,
            title: h ? h.textContent.trim() : null,
            image_src: img ? img.getAttribute('src') : null,
            image_srcset: img ? img.getAttribute('srcset') : null
        });
    }
    return { title: document.title, boards, pins };
}
"""


def _pin_from_browser_dict(d: Dict[str, Any]) -> Pin:
    return Pin(
        id=d["id"],
        url=d.get("url"),
        title=d.get("title") or None,
        image={
            "src": d.get("image_src"),
            "srcset": d.get("image_srcset"),
            "original": _original_from_srcset(d.get("image_srcset")),
        },
        source="blueprint-selector",
    )


def _original_from_srcset(srcset: Optional[str]) -> Optional[str]:
    if not srcset:
        return None
    import re

    m = re.search(
        r"(https://i\.pinimg\.com/originals/[^ \s]+\.(?:jpg|jpeg|png|webp))",
        srcset,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)
    entries = []
    for chunk in srcset.split(","):
        parts = chunk.strip().split()
        if not parts or not parts[0].startswith("http"):
            continue
        scale = 1.0
        if len(parts) >= 2:
            try:
                scale = float(parts[1].rstrip("x"))
            except ValueError:
                scale = 1.0
        entries.append((scale, parts[0]))
    if not entries:
        return None
    return sorted(entries, key=lambda x: x[0], reverse=True)[0][1]


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class PinterestScraper:
    """Top-level async scraper for Pinterest."""

    def __init__(self, mode: str = "auto", headless: bool = True) -> None:
        if mode not in ("auto", "fast", "full"):
            raise ValueError("mode must be 'auto', 'fast', or 'full'")
        self.mode = mode
        self._http: Optional[HttpClient] = None
        self._browser: Optional[BrowserManager] = None
        self._headless = headless

    # ---------------- lifecycle ----------------
    async def start(self) -> None:
        if self.mode in ("fast", "auto"):
            self._http = HttpClient()
            await self._http.start()
        if self.mode in ("full", "auto"):
            self._browser = BrowserManager(headless=self._headless)
            await self._browser.start()

    async def close(self) -> None:
        if self._http:
            await self._http.close()
            self._http = None
        if self._browser:
            await self._browser.close()
            self._browser = None

    async def __aenter__(self) -> "PinterestScraper":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ---------------- public API ----------------
    async def scrape_page(
        self,
        url: str,
        mode: Optional[str] = None,
        max_scrolls: Optional[int] = None,
    ) -> ScrapeResult:
        """Scrape a single URL and return its pins."""
        chosen = (mode or self.mode or "auto").lower()
        t0 = time.time()
        result: Optional[ScrapeResult] = None

        if chosen in ("fast", "auto") and self._http:
            try:
                html = await self._http.get(url)
                result = PinParser.parse_page(html, url, mode="fast")
            except Exception as exc:
                log.warning("fast scrape failed for %s: %s", url, exc)
                result = ScrapeResult(url=url, error=str(exc), mode="fast")

        if chosen in ("auto",) and (
            result is None or len(result.pins) < FAST_MIN_PINS_BEFORE_FALLBACK
        ):
            if self._browser is None and self.mode == "auto":
                # Lazily spin up the browser for the fallback.
                self._browser = BrowserManager(headless=self._headless)
                await self._browser.start()
            if self._browser:
                full = await self._scrape_full_url(url, max_scrolls=max_scrolls)
                if result is None or len(full.pins) > len(result.pins):
                    result = full
        elif chosen == "full" and self._browser:
            result = await self._scrape_full_url(url, max_scrolls=max_scrolls)

        if result is None:
            result = ScrapeResult(url=url, error="scraper not initialised", mode=chosen)

        result.duration_ms = int((time.time() - t0) * 1000)
        return result

    async def search_pins(
        self,
        query: str,
        pages: int = 2,
        mode: Optional[str] = None,
    ) -> List[Pin]:
        """Search for pins and concatenate results across multiple pages."""
        url = (
            f"{BASE_URL}/search/pins/?q={quote(query)}&rs=typed"
        )
        return await self._scrape_listing(url, pages=pages, mode=mode, label=f"search:{query}")

    async def scrape_board(
        self,
        username: str,
        board_slug: str,
        pages: int = 2,
        mode: Optional[str] = None,
    ) -> List[Pin]:
        """Scrape every pin from a public board."""
        url = f"{BASE_URL}/{username}/{board_slug}/"
        return await self._scrape_listing(
            url, pages=pages, mode=mode, label=f"board:{username}/{board_slug}"
        )

    async def scrape_profile(
        self,
        username: str,
        mode: Optional[str] = None,
    ) -> Profile:
        """Scrape a public profile (boards + first batch of pins)."""
        url = f"{BASE_URL}/{username}/"
        chosen = (mode or self.mode or "auto").lower()
        if chosen in ("fast", "auto") and self._http:
            try:
                html = await self._http.get(url)
                return ProfileParser.parse(html, username, mode="fast")
            except Exception as exc:
                log.warning("fast profile scrape failed: %s", exc)
                if chosen == "fast":
                    return Profile(username=username, title=username)
        if self._browser is None and self.mode == "auto":
            self._browser = BrowserManager(headless=self._headless)
            await self._browser.start()
        if not self._browser:
            return Profile(username=username, title=username)
        return await self._scrape_full_profile(username)

    async def export_result(
        self,
        result: ScrapeResult,
        json_name: str = "scrape.json",
        csv_name: Optional[str] = "scrape.csv",
    ) -> Dict[str, str]:
        """Persist a ScrapeResult to disk (JSON + optional CSV)."""
        paths: Dict[str, str] = {}
        paths["json"] = to_json(json_name, result.model_dump(mode="json"))
        if csv_name:
            paths["csv"] = to_csv(csv_name, (flatten_pin(p) for p in result.pins))
        return paths

    # ---------------- internals ----------------
    async def _scrape_listing(
        self,
        base_url: str,
        pages: int,
        mode: Optional[str],
        label: str,
    ) -> List[Pin]:
        all_pins: List[Pin] = []
        seen: set[str] = set()
        for i in range(1, pages + 1):
            sep = "&" if "?" in base_url else "?"
            page_url = f"{base_url}{sep}page={i}"
            log.info("[%s] page %d/%d → %s", label, i, pages, page_url)
            res = await self.scrape_page(page_url, mode=mode)
            for p in res.pins:
                if p.id in seen:
                    continue
                seen.add(p.id)
                all_pins.append(p)
            if not res.pins:
                break
            # respect rate limit between pages
            await asyncio.sleep(1.0)
        return all_pins

    async def _scrape_full_url(
        self,
        url: str,
        max_scrolls: Optional[int] = None,
    ) -> ScrapeResult:
        if self._browser is None:
            return ScrapeResult(url=url, error="browser not initialised", mode="full")

        scrolls = max_scrolls or settings.max_scrolls
        graphql_data: List[Dict[str, Any]] = []
        pins: List[Pin] = []
        seen: set[str] = set()

        try:
            async with self._browser.page() as page:
                page.on(
                    "response",
                    lambda r: asyncio.create_task(_maybe_capture(r, graphql_data)),
                )

                await goto_with_retry(page, url, timeout=30_000, retries=2)
                await wait_for_challenge_to_clear(page, max_wait_s=20.0)
                # Wait for either blueprint cards OR pin anchors to appear
                try:
                    await page.wait_for_selector(
                        ".ADXRXN, a[href*='/pin/']", timeout=15_000
                    )
                except Exception:
                    log.warning("No pin cards found on %s within timeout", url)

                for i in range(scrolls):
                    raw = await page.evaluate(EXTRACT_JS)
                    for d in raw:
                        pid = d.get("id")
                        if not pid or pid in seen:
                            continue
                        seen.add(pid)
                        pins.append(_pin_from_browser_dict(d))
                    log.info("[full] scroll %d/%d — %d pins so far", i + 1, scrolls, len(pins))
                    await page.evaluate("window.scrollBy(0, window.innerHeight * 0.9)")
                    await asyncio.sleep(settings.scroll_pause_s)
        except Exception as exc:
            # مهما فشل (timeout نهائي بعد إعادة المحاولات، تحدي لم يُحل، إلخ) —
            # نرجع ScrapeResult منظّم بدل ما نكسر الطلب بالكامل بـ exception
            # غير معالج، ونحتفظ بأي صور جُمعت قبل الفشل (أفضل من لا شيء).
            log.error("full scrape failed for %s: %s", url, exc)
            return ScrapeResult(
                url=url,
                pins=pins,
                graphql_responses=graphql_data,
                mode="full",
                error=str(exc),
            )

        return ScrapeResult(
            url=url,
            title="",
            pins=pins,
            graphql_responses=graphql_data,
            mode="full",
        )

    async def _scrape_full_profile(self, username: str) -> Profile:
        assert self._browser is not None
        try:
            async with self._browser.page() as page:
                await goto_with_retry(
                    page, f"{BASE_URL}/{username}/", timeout=30_000, retries=2
                )
                await wait_for_challenge_to_clear(page, max_wait_s=20.0)
                try:
                    await page.wait_for_selector(
                        ".ADXRXN, a[href*='/pin/']", timeout=15_000
                    )
                except Exception:
                    pass
                data = await page.evaluate(EXTRACT_PROFILE_JS, username)
        except Exception as exc:
            log.error("full profile scrape failed for %s: %s", username, exc)
            return Profile(username=username, title=username)
        boards = [Board(**b) for b in data.get("boards", [])]
        pins = [_pin_from_browser_dict(p) for p in data.get("pins", [])]
        return Profile(
            username=username,
            title=data.get("title") or username,
            boards=boards,
            pins=pins,
        )


async def _maybe_capture(response, sink: List[Dict[str, Any]]) -> None:
    if not is_graphql_response(response):
        return
    data = await safe_json(response)
    if data is not None:
        sink.append({"url": response.url, "data": data})
