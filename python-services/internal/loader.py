"""
internal/loader.py — محمّل Plugins الديناميكي.

يقابل منطق services() + تسجيل Routes في main.go لـ Go، لكن هنا
الاكتشاف تلقائي: كل ملف .py في مجلد plugins/ يُفحَص.
إن وجد متغير `plugin` من نوع Plugin → يُسجَّل.
إن لم يوجد أو حدث خطأ → يُتجاوَز مع تحذير في اللوق (لا يوقف التطبيق).

هذا النمط يتيح:
  - إضافة plugin جديد = ملف جديد في plugins/ فقط.
  - حذف plugin = حذف الملف.
  - لا تعديل في main.py أو أي ملف آخر.
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

from fastapi import APIRouter

from .plugin import Plugin, Route

log = logging.getLogger(__name__)

# مسار مجلد plugins/ — نسبي لهذا الملف (داخل الحزمة)
_PLUGINS_DIR = Path(__file__).parent.parent / "plugins"


def _load_one(path: Path) -> Plugin | None:
    """
    يحاول تحميل plugin من ملف .py واحد.
    يعيد None إن لم يوجد متغير `plugin` صالح أو حدث خطأ.
    """
    module_name = f"plugins.{path.stem}"

    try:
        # إذا كان محمَّلاً سابقاً، أعد تحميله (مفيد للتطوير)
        if module_name in sys.modules:
            mod = importlib.reload(sys.modules[module_name])
        else:
            mod = importlib.import_module(module_name)
    except Exception as exc:
        log.warning("❌ [%s] فشل الاستيراد: %s", path.stem, exc)
        return None

    plugin = getattr(mod, "plugin", None)
    if plugin is None:
        log.debug("⏭️  [%s] لا يوجد متغير `plugin` — متجاوَز", path.stem)
        return None

    if not isinstance(plugin, Plugin):
        log.warning(
            "⚠️  [%s] المتغير `plugin` ليس من نوع Plugin (نوعه: %s) — متجاوَز",
            path.stem,
            type(plugin).__name__,
        )
        return None

    return plugin


def discover_plugins() -> list[Plugin]:
    """
    يكتشف ويحمّل كل plugins من المجلد.
    الترتيب: أبجدي (حسب اسم الملف) — ثابت ومتوقع.
    """
    if not _PLUGINS_DIR.exists():
        log.warning("⚠️  مجلد plugins/ غير موجود: %s", _PLUGINS_DIR)
        return []

    loaded: list[Plugin] = []

    for path in sorted(_PLUGINS_DIR.glob("*.py")):
        if path.stem.startswith("_"):
            continue  # تجاهل __init__.py وكل ما يبدأ بـ _

        plugin = _load_one(path)
        if plugin is None:
            continue

        loaded.append(plugin)
        log.info("✅ [%s] محمَّل — %d route(s)", plugin.name(), len(plugin.routes()))

    log.info("📦 تم تحميل %d plugin(s) من %s", len(loaded), _PLUGINS_DIR)
    return loaded


def build_router(plugins: Sequence[Plugin]) -> APIRouter:
    """
    يبني APIRouter واحداً يجمع كل routes من كل plugins.
    يُضاف هذا Router لـ FastAPI app في main.py.
    """
    router = APIRouter()

    for plugin in plugins:
        for route in plugin.routes():
            router.add_api_route(
                path=route.path,
                endpoint=route.handler,
                methods=[route.method.upper()],
            )
            log.debug("  ↳ %s %s", route.method.upper(), route.path)

    return router
