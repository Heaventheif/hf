"""
plugins/gemini.py
endpoint: POST /gemini
Gemini 2.5 Flash (Google Search Grounding) — جلسات جماعية في MongoDB — Groq fallback
"""

import os
import httpx
from datetime import datetime
from fastapi import Request
from fastapi.responses import JSONResponse
from google import genai
from google.genai import types

DESCRIPTION = "Gemini 2.5 Flash (Google Search Grounding) — جلسات جماعية + Groq fallback"

# ─── Shared HTTP client (connection pooling) ──────────────────
_http = httpx.AsyncClient(
    timeout=25,
    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
)

GEMINI_KEYS = [k for k in [
    os.environ.get("GEMINI_API_KEY"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3"),
    os.environ.get("GEMINI_API_KEY_4"),
] if k and len(k) > 10]

GROQ_KEY  = os.environ.get("GROQ_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI", "")

SYSTEM = 'أنت بوت مساعد ذكي اسمك "Sunken". أجب باللغة العربية بإيجاز (أقل من 200 كلمة). كن ودوداً ومفيداً.'
MODEL_NAME = "gemini-2.5-flash"

GROUNDING_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM,
    temperature=0.7,
    max_output_tokens=1024,
    tools=[types.Tool(google_search=types.GoogleSearch())],
)

# ─── MongoDB ──────────────────────────────────────────────────
_db = None

async def _get_db():
    global _db
    if _db is not None:
        return _db
    if not MONGO_URI:
        return None
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _db = client["sunken"]["gemini_sessions"]
        return _db
    except Exception:
        return None

async def _load(thread_id: str) -> list:
    col = await _get_db()
    if not col:
        return []
    doc = await col.find_one({"_id": thread_id})
    return (doc or {}).get("messages", [])[-10:]

async def _save(thread_id: str, messages: list):
    col = await _get_db()
    if not col:
        return
    await col.update_one(
        {"_id": thread_id},
        {"$set": {"messages": messages[-10:], "updated_at": datetime.utcnow()}},
        upsert=True,
    )

async def _clear(thread_id: str):
    col = await _get_db()
    if col:
        await col.delete_one({"_id": thread_id})


# ─── Gemini ───────────────────────────────────────────────────
def _to_gemini_contents(messages: list) -> list:
    contents = []
    for m in messages:
        if m.get("role") == "system":
            continue
        gemini_role = "model" if m["role"] == "assistant" else "user"
        contents.append(
            types.Content(role=gemini_role, parts=[types.Part(text=m["content"])])
        )
    return contents

async def _call_gemini(messages: list) -> str:
    contents = _to_gemini_contents(messages)
    for key in GEMINI_KEYS:
        try:
            client = genai.Client(api_key=key)
            response = await client.aio.models.generate_content(
                model=MODEL_NAME,
                contents=contents,
                config=GROUNDING_CONFIG,
            )
            reply = (response.text or "").strip()
            if reply:
                return reply
        except Exception as e:
            msg = str(e).lower()
            if "429" in msg or "quota" in msg or "resource_exhausted" in msg:
                continue
            continue
    raise RuntimeError("ALL_GEMINI_KEYS_EXHAUSTED")

async def _call_groq(messages: list) -> str:
    if not GROQ_KEY:
        raise RuntimeError("NO_GROQ_KEY")
    r = await _http.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
        json={"model": "llama-3.3-70b-versatile", "messages": messages, "max_tokens": 1024, "temperature": 0.7}
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def register(app):

    @app.post("/gemini")
    async def gemini_endpoint(request: Request):
        """
        Body: {
          "thread_id":   "group_123",
          "sender_name": "Ahmed",
          "prompt":      "سؤالك",
          "clear":       false
        }
        أو النمط القديم: { "messages": [...] }  (بدون جلسات)
        Response: { "reply": "...", "provider": "gemini|groq" }
        """
        try:
            body = await request.json()

            # ─── نمط الجلسات الجماعية (الجديد) ──────────────
            if "thread_id" in body or "prompt" in body:
                thread_id   = body.get("thread_id", "default")
                sender_name = body.get("sender_name", "مستخدم")
                prompt      = body.get("prompt", "").strip()
                do_clear    = body.get("clear", False)

                if do_clear:
                    await _clear(thread_id)
                    return JSONResponse({"reply": "🧹 تم مسح ذاكرة المجموعة."})

                if not prompt:
                    return JSONResponse({"error": "prompt مطلوب"}, status_code=400)

                ctx = await _load(thread_id)
                user_content = f"[{sender_name}]: {prompt}"

                messages = [
                    {"role": "system",    "content": SYSTEM},
                    *ctx,
                    {"role": "user",      "content": user_content},
                ]

                try:
                    reply = await _call_gemini(messages)
                    provider = "gemini"
                except Exception:
                    try:
                        groq_msgs = messages if any(m["role"] == "system" for m in messages) else \
                                    [{"role": "system", "content": SYSTEM}] + messages
                        reply = await _call_groq(groq_msgs)
                        provider = "groq"
                    except Exception as e2:
                        return JSONResponse({"error": f"كل الخوادم فشلت: {str(e2)[:100]}"}, status_code=503)

                await _save(thread_id, [
                    *ctx,
                    {"role": "user",      "content": user_content},
                    {"role": "assistant", "content": reply},
                ])
                return JSONResponse({"reply": reply, "provider": provider})

            # ─── النمط القديم: messages مباشرة (بدون جلسات) ─
            messages = body.get("messages", [])
            if not messages:
                return JSONResponse({"error": "messages أو prompt مطلوب"}, status_code=400)

            try:
                reply = await _call_gemini(messages)
                return JSONResponse({"reply": reply, "provider": "gemini"})
            except Exception:
                try:
                    groq_messages = messages if any(m.get("role") == "system" for m in messages) else \
                                    [{"role": "system", "content": SYSTEM}] + messages
                    reply = await _call_groq(groq_messages)
                    return JSONResponse({"reply": reply, "provider": "groq"})
                except Exception as e2:
                    return JSONResponse({"error": f"كل الخوادم فشلت: {str(e2)[:100]}"}, status_code=503)

        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)
