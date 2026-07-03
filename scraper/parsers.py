"""HTML / JSON parsers.

The selectors come straight from the Scraping Blueprint report:
  - Item container:  .ADXRXN
  - Title:            h1.wyEmcc   (fallback: h1,h2,h3,[class*="title"])
  - Link:             a.etmDmh    (fallback: a[href*="/pin/"])
  - Image:            img.iFOUS5   (+ srcset → /originals/... extraction)
"""
from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .models import Pin, Board, Profile, ScrapeResult

BASE_URL = "https://www.pinterest.com"

# Matches the 2x entry of a Pinterest srcset that points to the original asset.
ORIGINAL_RE = re.compile(
    r"(https://i\.pinimg\.com/originals/[^ \s]+\.(?:jpg|jpeg|png|webp))", re.IGNORECASE
)
PIN_ID_RE = re.compile(r"/pin/(\d+)")
PAGE_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)


# ----------------------------------------------------------------------
# Generic helpers
# ----------------------------------------------------------------------


def _abs_url(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return BASE_URL + href
    return urljoin(BASE_URL + "/", href)


def _pick_original(srcset: Optional[str]) -> Optional[str]:
    if not srcset:
        return None
    m = ORIGINAL_RE.search(srcset)
    if m:
        return m.group(1)
    # Fallback: highest-resolution entry.
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


def _pin_from_card(card: Tag, source: str = "blueprint-selector") -> Optional[Pin]:
    """Build a Pin from a card-like element."""
    anchor = card.select_one('a[href*="/pin/"]')
    if not anchor or not anchor.has_attr("href"):
        return None
    href = anchor["href"]
    m = PIN_ID_RE.search(href)
    if not m:
        return None

    img = card.select_one("img")
    src = img.get("src") if img else None
    srcset = img.get("srcset") if img else None

    title_el = card.select_one("h1, h2, h3, [class*='title']")
    title = title_el.get_text(strip=True) if title_el else None

    return Pin(
        id=m.group(1),
        url=_abs_url(href),
        title=title or None,
        image={"src": src, "srcset": srcset, "original": _pick_original(srcset)},
        source=source,
    )


# ----------------------------------------------------------------------
# Public parsers
# ----------------------------------------------------------------------


class PinParser:
    """Parse one Pinterest page and return its pins."""

    @staticmethod
    def parse_page(html: str, url: str, mode: str = "fast") -> ScrapeResult:
        soup = BeautifulSoup(html or "", "lxml")
        title_el = soup.find("title")
        title = title_el.get_text(strip=True) if title_el else None

        pins: List[Pin] = []
        seen: set[str] = set()

        # 1. Blueprint selector: .ADXRXN cards
        for card in soup.select(".ADXRXN"):
            pin = _pin_from_card(card, "blueprint-selector")
            if pin and pin.id not in seen:
                seen.add(pin.id)
                pins.append(pin)

        # 2. Fallback: any anchor that points to a /pin/<id>/
        if not pins:
            for a in soup.select('a[href*="/pin/"]'):
                if not a.has_attr("href"):
                    continue
                card = a
                # Walk up to a sensible container
                for _ in range(4):
                    if card.parent and card.parent.name in ("div", "li", "article", "section"):
                        card = card.parent
                    else:
                        break
                pin = _pin_from_card(card, "fallback")
                if pin and pin.id not in seen:
                    seen.add(pin.id)
                    pins.append(pin)

        return ScrapeResult(url=url, title=title, pins=pins, mode=mode)


class ProfileParser:
    """Parse a public profile page."""

    @staticmethod
    def parse(html: str, username: str, mode: str = "fast") -> Profile:
        soup = BeautifulSoup(html or "", "lxml")
        title_el = soup.select_one("h1.wyEmcc, h1")
        title = title_el.get_text(strip=True) if title_el else username

        boards: List[Board] = []
        seen_slugs: set[str] = set()
        skip_slugs = {"pins", "saved", "_saved", "today"}

        # Boards are linked as /<username>/<slug>/
        pattern = re.compile(rf"/{re.escape(username)}/([^/?#]+)/?$")
        for a in soup.select(f'a[href*="/{username}/"]'):
            href = a.get("href", "")
            m = pattern.search(href)
            if not m:
                continue
            slug = m.group(1)
            if slug in skip_slugs or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            boards.append(
                Board(
                    slug=slug,
                    url=_abs_url(href),
                    title=a.get_text(strip=True)[:120] or None,
                )
            )

        pins: List[Pin] = []
        page_result = PinParser.parse_page(html, f"{BASE_URL}/{username}/", mode=mode)
        pins = page_result.pins

        return Profile(username=username, title=title, boards=boards, pins=pins)
