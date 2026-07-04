"""scraper/cookies.py

يقرأ ملف كوكيز بصيغة Netscape (المُصدَّر من متصفح) ويستخرج **فقط** كوكيز
pinterest.com (وأي subdomain تابع له) — نتجاهل كل الدومينات الأخرى الموجودة
في الملف عمداً (حتى لو الملف الأصلي يحتوي كوكيز مواقع ثانية).

الصيغة المتوقعة لكل سطر (tab-separated):
    domain  include_subdomains  path  secure  expires  name  value
"""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional, TypedDict

log = logging.getLogger(__name__)

PINTEREST_SUFFIX = "pinterest.com"


class PlaywrightCookie(TypedDict, total=False):
    name: str
    value: str
    domain: str
    path: str
    expires: float
    httpOnly: bool
    secure: bool
    sameSite: str


def _is_pinterest_domain(domain: str) -> bool:
    d = domain.lstrip(".").lower()
    return d == PINTEREST_SUFFIX or d.endswith("." + PINTEREST_SUFFIX)


def load_pinterest_cookies(path: Optional[str] = None) -> List[PlaywrightCookie]:
    """يقرأ ملف كوكيز Netscape ويرجّع قائمة كوكيز pinterest.com فقط،
    بصيغة جاهزة لـ Playwright's ``context.add_cookies()``.

    لا يرمي استثناء لو الملف غير موجود — يرجّع قائمة فارغة ويسجّل تحذير،
    حتى لا يكسر تشغيل السكربر لو نسي أحد رفع الملف.
    """
    path = path or os.getenv("SCRAPER_COOKIES_FILE", "cookies.txt")
    if not path or not os.path.isfile(path):
        log.warning("[cookies] ملف الكوكيز غير موجود: %s — سيعمل السكربر بدون تسجيل دخول", path)
        return []

    cookies: List[PlaywrightCookie] = []
    now = time.time()
    seen: set[tuple[str, str, str]] = set()

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 7:
                continue
            domain, _include_sub, cpath, secure_flag, expires_raw, name, value = parts
            if not _is_pinterest_domain(domain):
                continue

            try:
                expires = float(expires_raw)
            except ValueError:
                expires = 0.0
            # كوكيز منتهية الصلاحية لا فائدة منها — نتجاهلها.
            if expires and expires < now:
                continue

            key = (domain, cpath, name)
            if key in seen:
                continue
            seen.add(key)

            cookie: PlaywrightCookie = {
                "name": name,
                "value": value,
                "domain": domain if domain.startswith(".") else domain,
                "path": cpath or "/",
                "secure": secure_flag.upper() == "TRUE",
            }
            if expires:
                cookie["expires"] = expires
            cookies.append(cookie)

    log.info("[cookies] تحميل %d كوكيز pinterest.com من %s", len(cookies), path)
    return cookies


def cookies_as_header_dict(cookies: List[PlaywrightCookie]) -> Dict[str, str]:
    """يحوّل قائمة كوكيز Playwright إلى dict بسيط name->value، مفيد لـ
    curl_cffi / أي عميل HTTP خفيف (fast mode) بدل متصفح كامل."""
    out: Dict[str, str] = {}
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if name and value is not None:
            out[name] = value
    return out
