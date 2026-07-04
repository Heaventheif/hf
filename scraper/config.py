"""Runtime configuration for the scraper.

All values are tunable through environment variables — see the ``Settings``
class below.  The defaults are tuned to be safe for small/medium jobs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _split_env(name: str, default: str) -> List[str]:
    raw = os.getenv(name, default)
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass
class Settings:
    # ---- HTTP / network -------------------------------------------------
    min_delay_ms: int = int(os.getenv("SCRAPER_MIN_DELAY_MS", "1200"))
    max_delay_ms: int = int(os.getenv("SCRAPER_MAX_DELAY_MS", "2500"))
    timeout_s: int = int(os.getenv("SCRAPER_TIMEOUT_S", "30"))
    max_retries: int = int(os.getenv("SCRAPER_MAX_RETRIES", "4"))

    # Pool of recent desktop Chrome User-Agents
    user_agents: List[str] = field(
        default_factory=lambda: _split_env(
            "SCRAPER_USER_AGENTS",
            (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0.0.0 Safari/537.36,"
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36,"
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
            ),
        )
    )

    # ---- Playwright / browser ------------------------------------------
    headless: bool = os.getenv("SCRAPER_HEADLESS", "true").lower() == "true"
    browser_path: str = os.getenv(
        "PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright"
    )  # matches the Dockerfile
    max_scrolls: int = int(os.getenv("SCRAPER_MAX_SCROLLS", "6"))
    scroll_pause_s: float = float(os.getenv("SCRAPER_SCROLL_PAUSE_S", "1.5"))

    # ---- Modes ----------------------------------------------------------
    # auto = fast first, fall back to full if too few pins
    # fast = curl_cffi / cloudscraper only
    # full = Playwright headless browser
    default_mode: str = os.getenv("SCRAPER_MODE", "auto")

    # ---- Auth / cookies ---------------------------------------------------
    # مسار ملف كوكيز Netscape — تُستخرج منه فقط كوكيز pinterest.com
    # (انظر scraper/cookies.py). القيمة الافتراضية تفترض أن الملف بجانب main.py.
    cookies_file: str = os.getenv("SCRAPER_COOKIES_FILE", "cookies.txt")

    # ---- API / persistence ---------------------------------------------
    data_dir: str = os.getenv("SCRAPER_DATA_DIR", "./data")
    log_level: str = os.getenv("SCRAPER_LOG_LEVEL", "INFO")


settings = Settings()
