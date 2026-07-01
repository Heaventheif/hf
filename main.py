"""
main.py — ثابت نهائياً، لا يُعدَّل أبداً
كل الـ endpoints تُضاف عبر plugins/
"""
import asyncio
import time
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from plugin_loader import load_all_plugins

# ─── تفعيل logging ليظهر في لوق HF ──────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)

app = FastAPI(title="Universal Bot API", version="3.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"]
)

@app.get("/")
async def root():
    from plugin_loader import get_registry
    return {
        "status":  "online",
        "plugins": get_registry(),
    }

@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": time.time()}

# ─── تحميل كل الـ plugins عند البداية ───────────────
load_all_plugins(app)
