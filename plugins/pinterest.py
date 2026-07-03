"""
plugins/pinterest.py — Pinterest image search + HD download.

Drops into the existing plugin system of the Sunken Bot API (hf-space).
Uses the Playwright-based scraper that already lives in `scraper/`.

Endpoint:
    POST /pinterest
    Body: { "query": "cat", "limit": 5, "quality": "original|736|474|236",
            "username": "pinterest", "board_slug": "official", "fallback_ferdev": true }
    Response: {
        "success": true,
        "query": "cat",
        "count": 5,
        "images": [
            { "id": "...", "url": "https://i.pinimg.com/originals/...",
              "title": "...", "source_url": "https://www.pinterest.com/pin/...",
              "width": null, "height": null, "thumbnail": "..." }
        ],
        "provider": "playwright" | "ferdev" | "hybrid"
    }

Falls back to FERDEV_API_KEY (legacy endpoint) if the local scraper fails
or returns nothing, so the bot never breaks for the end user.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from typing import List, Optional

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field

DESCRIPTION = "بحث وتحميل صور عالية الدقة من Pinterest (Playwright + Ferdev fallback)"
DOCKERFILE_DEPS: list = []  # Playwright already installed by base Dockerfile

# Lazy-initialised scraper singleton (one per worker process).
_scraper_state: dict = {}


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class PinterestRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(5, ge=1, le=20)
    # Image size — "original" (HD), 736, 474, 236 (thumbnail)
    quality: str = Field("original", pattern="^(original|736|474|236)$")
    # Optional: scrape a specific board instead of a keyword
    username: Optional[str] = None
    board_slug: Optional[str] = None
    # When True, return base64-encoded images (slower, but useful for tiny
    # proxies that cannot follow Pinterest's CDN).  Default = False.
    as_base64: bool = False
    # When True, fall back to FERDEV_API_KEY on scraper failure.
    fallback_ferdev: bool = True


class ImageResult(BaseModel):
    id: str
    url: str
    title: Optional[str] = None
    source_url: Optional[str] = None
    thumbnail: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


class PinterestResponse(BaseModel):
    success: bool
    query: Optional[str] = None
    count: int
    images: List[ImageResult] = []
    provider: str = "unknown"
    error: Optional[str] = None
    elapsed_ms: int = 0


# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------

ORIGINAL_RE = re.compile(
    r"(https://i\.pinimg\.com/originals/[^ \s,]+\.(?:jpg|jpeg|png|webp))",
    re.IGNORECASE,
)
PIN_ID_RE = re.compile(r"/pin/(\d+)")


def _convert_quality(url: str, quality: str) -> str:
    """Switch a Pinterest CDN URL to a different size.

    Pinterest URLs look like:
        https://i.pinimg.com/originals/<hash>/<file>.jpg
        https://i.pinimg.com/736x/<hash>/<file>.jpg
        https://i.pinimg.com/474x/<hash>/<file>.jpg
        https://i.pinimg.com/236x/<hash>/<file>.jpg
    """
    if not url or quality == "original":
        return url
    pattern = re.compile(r"https://i\.pinimg\.com/(?:originals|736x|474x|236x)/")
    return pattern.sub(f"https://i.pinimg.com/{quality}/", url)


async def _get_scraper():
    """Lazy singleton — the Playwright browser is heavy, share it."""
    if _scraper_state.get("scraper") is None:
        from scraper import PinterestScraper  # type: ignore

        s = PinterestScraper(mode="auto")
        await s.start()
        _scraper_state["scraper"] = s
        _scraper_state["lock"] = asyncio.Lock()
    return _scraper_state["scraper"]


def _pin_to_image(pin, quality: str) -> Optional[ImageResult]:
    if not pin or not getattr(pin, "image", None):
        return None
    img = pin.image or {}
    url = img.get("original") or img.get("src")
    if not url:
        return None
    return ImageResult(
        id=pin.id,
        url=_convert_quality(url, quality),
        title=pin.title,
        source_url=str(pin.url) if pin.url else None,
        thumbnail=img.get("src"),
    )


# ---------------------------------------------------------------------------
# Ferdev fallback (legacy)
# ---------------------------------------------------------------------------

async def _ferdev_search(query: str, limit: int, api_key: str) -> List[ImageResult]:
    """Hit Ferdev's pinterest endpoint — kept as a safety net."""
    if not api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                "https://api.ferdev.my.id/search/pinterest",
                params={"query": query, "apikey": api_key, "limit": limit},
            )
            data = r.json()
        results = data.get("result") or data.get("data") or data.get("results") or []
        out: List[ImageResult] = []
        for i, item in enumerate(results[:limit]):
            url = (
                item.get("image")
                or item.get("url")
                or item.get("imageUrl")
                or (item.get("images") or [None])[0]
            )
            if not url:
                continue
            pin_id = (
                item.get("id")
                or (item.get("pin") or {}).get("id")
                or f"ferdev_{i}"
            )
            out.append(
                ImageResult(
                    id=str(pin_id),
                    url=url,
                    title=item.get("title") or item.get("description"),
                    source_url=item.get("source") or item.get("link"),
                    thumbnail=item.get("thumbnail"),
                )
            )
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"[pinterest] ferdev fallback failed: {exc}")
        return []


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(app):

    @app.post("/pinterest", response_model=PinterestResponse)
    async def pinterest_search(req: PinterestRequest) -> PinterestResponse:
        t0 = time.time()
        provider = "playwright"
        images: List[ImageResult] = []

        # ---- 1. Try local Playwright scraper ----
        try:
            scraper = await _get_scraper()
            lock = _scraper_state["lock"]
            async with lock:
                if req.username and req.board_slug:
                    # ~25 pins per page
                    pages = max(1, (req.limit + 24) // 25)
                    pins = await scraper.scrape_board(
                        req.username, req.board_slug, pages=pages
                    )
                else:
                    pages = max(1, (req.limit + 24) // 25)
                    pins = await scraper.search_pins(
                        req.query, pages=pages
                    )

            for p in pins[: req.limit]:
                img = _pin_to_image(p, req.quality)
                if img:
                    images.append(img)
        except Exception as exc:  # noqa: BLE001
            provider = "playwright-error"
            err = str(exc)
        else:
            err = None

        # ---- 2. Fall back to Ferdev if needed ----
        if not images and req.fallback_ferdev and not (req.username and req.board_slug):
            api_key = os.getenv("FERDEV_API_KEY")
            ferdev_imgs = await _ferdev_search(req.query, req.limit, api_key)
            if ferdev_imgs:
                images = ferdev_imgs
                provider = "ferdev"

        # ---- 3. Optional base64 encoding ----
        if req.as_base64 and images:
            async with httpx.AsyncClient(timeout=30) as client:
                for img in images:
                    try:
                        r = await client.get(img.url, timeout=20)
                        if r.status_code == 200:
                            import base64
                            b64 = base64.b64encode(r.content).decode("ascii")
                            mime = "image/jpeg"
                            if img.url.lower().endswith(".png"):
                                mime = "image/png"
                            elif img.url.lower().endswith(".webp"):
                                mime = "image/webp"
                            img.thumbnail = f"data:{mime};base64,{b64[:120]}..."
                    except Exception:
                        pass

        if not images and not err:
            err = "no images found"

        elapsed = int((time.time() - t0) * 1000)
        return PinterestResponse(
            success=bool(images),
            query=req.query,
            count=len(images),
            images=images,
            provider=provider,
            error=err,
            elapsed_ms=elapsed,
        )

    @app.get("/pinterest/health")
    async def pinterest_health():
        """Cheap health check — does NOT spawn the browser."""
        return {
            "ok": True,
            "browser_initialised": _scraper_state.get("scraper") is not None,
            "ferdev_key_present": bool(os.getenv("FERDEV_API_KEY")),
        }
