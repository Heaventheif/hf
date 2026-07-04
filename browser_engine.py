"""
browser_engine.py
Advanced anti‑detection browser engine using nodriver + curl_cffi.
Bypasses Cloudflare Turnstile, Akamai, PerimeterX.
"""

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import nodriver as uc
from curl_cffi import requests as cffi_requests

log = logging.getLogger("browser_engine")

# ---------- User‑agents & headers ----------
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
]
SEC_CH_UA = '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'

def generate_curl_headers() -> Dict[str, str]:
    ua = random.choice(USER_AGENTS)
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": SEC_CH_UA,
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }


# ---------- Finder for Playwright's Chromium ----------
def find_playwright_chromium() -> str:
    """Find the Chromium binary installed by Playwright."""
    for base in (Path("/ms-playwright"), Path.home() / ".cache/ms-playwright"):
        if base.exists():
            for binary in base.glob("chromium-*/chrome-linux/chrome"):
                return str(binary)
    raise FileNotFoundError("Playwright Chromium not found. Run 'playwright install chromium'.")


class AntiDetectionBrowser:
    """
    High‑level interface to launch a browser with nodriver,
    solve challenges, perform human‑like interactions, and export
    cookies usable in curl_cffi for high‑speed downloads.
    """

    def __init__(
        self,
        headless: bool = True,
        browser_executable: Optional[str] = None,
        proxy: Optional[str] = None,
    ):
        self.headless = headless
        self.browser_executable = browser_executable or find_playwright_chromium()
        self.proxy = proxy
        self.browser: Optional[uc.Browser] = None
        self.page: Optional[uc.Tab] = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def start(self):
        """Launch the browser."""
        args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
        ]
        if self.proxy:
            args.append(f"--proxy-server={self.proxy}")
        self.browser = await uc.start(
            headless=self.headless,
            browser_executable_path=self.browser_executable,
            no_sandbox=True,
            browser_args=args,
        )

    async def close(self):
        """Shut down the browser gracefully."""
        if self.browser:
            try:
                self.browser.stop()
            except Exception:
                pass

    async def visit(self, url: str, timeout: int = 30) -> str:
        """
        Navigate to URL, wait for DOM, solve challenges, and
        return the final page HTML.
        """
        if not self.browser:
            raise RuntimeError("Browser not started. Call start() first.")
        self.page = await self.browser.get(url, new_tab=True)
        await self.page.wait_for("body", timeout=timeout)
        await self._solve_challenges()
        await self._human_interact()
        return await self.page.get_content()

    async def get_cookies(self) -> Dict[str, str]:
        """Return all cookies as a dict (name: value)."""
        if not self.page:
            return {}
        cookies = await self.page.cookies.get_all()
        return {c.name: c.value for c in cookies if c.name and c.value}

    # ---- internal helpers ----
    async def _solve_challenges(self):
        """
        Wait for Cloudflare / common WAF challenges to clear.
        """
        max_attempts = 6
        for attempt in range(max_attempts):
            title = await self.page.evaluate("document.title")
            body = await self.page.evaluate("document.body.innerText")
            combined = (title + " " + body).lower()
            blocked = ["just a moment", "attention required", "checking your browser", "ddos protection"]
            if not any(ind in combined for ind in blocked):
                return True

            # هل فيه Turnstile widget فعلي بالصفحة (checkbox تفاعلي)؟
            has_turnstile = await self.page.evaluate(
                """!!document.querySelector('iframe[src*="challenges.cloudflare.com"], '
                   + '.cf-turnstile, #cf-turnstile')"""
            )
            if has_turnstile:
                log.warning(
                    "Turnstile تفاعلي مكتشف (محاولة %d/%d) — هذا يحتاج تدخل "
                    "بشري أو خدمة حل خارجية، الانتظار وحده لن يحله.",
                    attempt + 1, max_attempts,
                )
            else:
                log.info("تحدي تلقائي مكتشف (محاولة %d/%d)، بالانتظار...", attempt + 1, max_attempts)

            await asyncio.sleep(5 + random.uniform(0, 2))

        log.warning("Challenge may not be solved after %d retries.", max_attempts)
        return False

    async def _human_interact(self):
        """Perform randomized scrolling and mouse movements."""
        try:
            await asyncio.sleep(random.uniform(2, 4))
            scroll_h = await self.page.evaluate("document.body.scrollHeight")
            if scroll_h:
                steps = random.randint(4, 8)
                for i in range(1, steps + 1):
                    target = (i * scroll_h) / steps
                    await self.page.evaluate(
                        f"window.scrollTo({{ top: {target}, behavior: 'smooth' }})"
                    )
                    await asyncio.sleep(random.uniform(0.2, 0.6))
                await self.page.evaluate(f"window.scrollTo({{ top: {scroll_h * 0.2}, behavior: 'smooth' }})")
                await asyncio.sleep(random.uniform(0.5, 1.5))
            for _ in range(random.randint(2, 4)):
                x, y = random.randint(200, 800), random.randint(200, 700)
                await self.page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.1, 0.3))
        except Exception as e:
            log.debug(f"Interaction error (non‑fatal): {e}")


# ---- curl_cffi session factory ----
def create_curl_session(cookies: Dict[str, str], proxy: Optional[Dict[str, str]] = None):
    """Return a curl_cffi SyncSession with Chrome impersonation and cookies."""
    session = cffi_requests.Session(impersonate="chrome120")
    session.cookies.update(cookies)
    session.headers.update(generate_curl_headers())
    if proxy:
        session.proxies = proxy
    return session


async def async_download_example(url: str, cookies: dict, save_path: Path, proxy=None):
    """Example async download using curl_cffi."""
    async with cffi_requests.AsyncSession(impersonate="chrome120") as sess:
        sess.cookies.update(cookies)
        sess.headers.update(generate_curl_headers())
        if proxy:
            sess.proxies = proxy
        resp = await sess.get(url)
        if resp.status_code == 200:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(resp.content)
            return True
    return False