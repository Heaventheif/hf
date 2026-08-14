"""
main.py — نقطة الدخول الوحيدة.

المسؤوليات:
  1. تشغيل lifespan (startup/shutdown لكل plugin).
  2. اكتشاف وتحميل plugins ديناميكياً من plugins/.
  3. تسجيل middleware الحماية (X-Internal-Token).
  4. توفير GET / و GET /health و GET /ping.

إضافة plugin جديد = ملف جديد في plugins/ فقط.
لا تعديل هنا.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from internal.auth import make_auth_middleware
from internal.loader import build_router, discover_plugins

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

_start_time = time.time()

# ─── اكتشاف Plugins (يحدث مرة واحدة عند import) ─────────────────────────────
# discover_plugins() يقرأ المجلد ويحمّل الملفات — آمن وقت import
# لأن النماذج الثقيلة تُحمَّل في startup() وليس عند import.
_plugins = discover_plugins()


# ─── Lifespan: startup + shutdown ────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: استدعاء startup() لكل plugin
    for p in _plugins:
        try:
            p.startup()
        except Exception as exc:
            log.error("❌ [%s] فشل startup(): %s", p.name(), exc)

    log.info("🚀 SunkenBot-Python جاهز — %d plugin(s) محمَّل", len(_plugins))
    yield

    # Shutdown: استدعاء shutdown() لكل plugin
    for p in _plugins:
        try:
            p.shutdown()
        except Exception as exc:
            log.warning("⚠️  [%s] خطأ في shutdown(): %s", p.name(), exc)


# ─── FastAPI app ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="SunkenBot — Python Services",
    version="2.0.0",
    docs_url=None,   # أغلق Swagger في production
    redoc_url=None,
    lifespan=lifespan,
)

# Middleware: حماية X-Internal-Token (يجب إضافته قبل Router)
app.add_middleware(make_auth_middleware(app).__class__, token=os.environ.get("INTERNAL_TOKEN", ""))

# Router: كل routes من كل plugins
app.include_router(build_router(_plugins))


# ─── Public endpoints ─────────────────────────────────────────────────────────

@app.get("/")
async def root():
    """
    GET / — معلومات عامة عن الخدمة وحالة كل plugin.
    يقابل GET / في main.go لـ Go.
    مستثنى من حماية التوكن.
    """
    plugin_info = {}
    for p in _plugins:
        routes_list = [f"{rt.method} {rt.path}" for rt in p.routes()]
        plugin_info[p.name()] = {
            "description": p.description,
            "routes": routes_list,
            "routes_count": len(routes_list),
            **p.status(),
        }

    return {
        "status": "online",
        "service": "sunkenbot-python",
        "version": "2.0.0",
        "uptime_seconds": round(time.time() - _start_time),
        "plugins": plugin_info,
    }


@app.get("/health")
async def health():
    """
    GET /health — فحص صحة سريع.
    يقابل GET /health في main.go لـ Go.
    مستثنى من حماية التوكن.
    """
    return {"status": "healthy", "timestamp": int(time.time())}


@app.get("/ping")
async def ping():
    """
    GET /ping — keep-alive من Render أو Go.
    مستثنى من حماية التوكن.
    """
    return {"ping": "pong"}
