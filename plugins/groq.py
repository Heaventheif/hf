"""
plugins/groq.py
endpoint: POST /groq
Llama 4 Scout — Vision + Audio + Video + جلسات جماعية MongoDB + Gemini fallback
"""

import os, base64, httpx, asyncio
from datetime import datetime
from fastapi import Request
from fastapi.responses import JSONResponse

DESCRIPTION = "Llama 4 Scout — Vision + Audio + Video + جلسات جماعية + Gemini fallback"

# ─── Shared HTTP clients (connection pooling) ─────────────────
_http = httpx.AsyncClient(
    timeout=30,
    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
)
_http_dl = httpx.AsyncClient(
    timeout=120,
    follow_redirects=True,
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)

GROQ_KEY = os.environ.get("GROQ_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI", "")
GEMINI_KEYS = [k for k in [
    os.environ.get("GEMINI_API_KEY"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3"),
    os.environ.get("GEMINI_API_KEY_4"),
] if k and len(k) > 10]

LLAMA4_MODEL  = "meta-llama/llama-4-scout-17b-16e-instruct"
WHISPER_MODEL = "whisper-large-v3"

SYSTEM = (
    'أنت بوت مساعد ذكي اسمك "Sunken". '
    'أجب دائماً باللغة العربية بإيجاز (أقل من 300 كلمة). '
    'كن ودوداً ومهذباً. إذا أُرسلت إليك صورة أو صوت أو فيديو فحللها بدقة.'
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
        _db = client["sunken"]["groq_sessions"]
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


# ─── Media helpers ────────────────────────────────────────────
async def _fetch_base64(url: str) -> tuple[bytes, str]:
    r = await _http_dl.get(url, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    raw = r.content
    return raw, base64.b64encode(raw).decode()

def _guess_mime(url: str, raw: bytes) -> str:
    url_low = url.lower().split("?")[0]
    if url_low.endswith(".png"):  return "image/png"
    if url_low.endswith(".gif"):  return "image/gif"
    if url_low.endswith(".webp"): return "image/webp"
    if url_low.endswith(".mp3"):  return "audio/mp3"
    if url_low.endswith(".m4a"):  return "audio/mp4"
    if url_low.endswith(".ogg"):  return "audio/ogg"
    if url_low.endswith(".wav"):  return "audio/wav"
    if url_low.endswith(".mp4"):  return "video/mp4"
    if raw[:4] == b'\x89PNG':    return "image/png"
    if raw[:3] == b'GIF':        return "image/gif"
    if raw[:2] in (b'\xff\xd8',):return "image/jpeg"
    if raw[:4] == b'RIFF':       return "audio/wav"
    if raw[:3] == b'ID3':        return "audio/mp3"
    return "image/jpeg"

# ─── Groq calls ──────────────────────────────────────────────
async def _groq_text(messages: list) -> str:
    if not GROQ_KEY:
        raise RuntimeError("NO_GROQ_KEY")
    r = await _http.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}"},
        json={"model": LLAMA4_MODEL, "messages": messages, "max_tokens": 1024, "temperature": 0.7}
    )
    r.raise_for_status()
    reply = r.json()["choices"][0]["message"]["content"]
    if not reply: raise RuntimeError("EMPTY")
    return reply

async def _groq_vision(messages: list, img_b64: str, mime: str) -> str:
    if not GROQ_KEY:
        raise RuntimeError("NO_GROQ_KEY")
    groq_msgs = []
    for i, m in enumerate(messages):
        if i == len(messages) - 1 and m["role"] == "user":
            text = m["content"] if isinstance(m["content"], str) else ""
            groq_msgs.append({"role": "user", "content": [
                {"type": "text", "text": text or "وصف هذه الصورة"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
            ]})
        else:
            groq_msgs.append(m)
    r = await _http.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}"},
        json={"model": LLAMA4_MODEL, "messages": groq_msgs, "max_tokens": 1024},
        timeout=45,
    )
    r.raise_for_status()
    reply = r.json()["choices"][0]["message"]["content"]
    if not reply: raise RuntimeError("EMPTY")
    return reply

async def _groq_audio(audio_raw: bytes, mime: str, prompt: str) -> str:
    if not GROQ_KEY:
        raise RuntimeError("NO_GROQ_KEY")
    ext_map = {
        "audio/mp3": "mp3", "audio/mpeg": "mp3", "audio/mp4": "m4a",
        "audio/m4a": "m4a", "audio/ogg": "ogg", "audio/wav": "wav",
        "audio/webm": "webm", "audio/flac": "flac",
    }
    ext = ext_map.get(mime, "mp3")
    r = await _http.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {GROQ_KEY}"},
        files={"file": (f"audio.{ext}", audio_raw, mime)},
        data={"model": WHISPER_MODEL, "language": "ar", "response_format": "text"},
        timeout=60,
    )
    r.raise_for_status()
    transcription = r.text.strip()
    if not transcription:
        raise RuntimeError("EMPTY_TRANSCRIPTION")
    follow_up = prompt.strip() or "لخص ما قيل في هذا الصوت"
    text_msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user",   "content": f"[تفريغ الصوت]: {transcription}\n\nالسؤال: {follow_up}"},
    ]
    reply = await _groq_text(text_msgs)
    return f"🎵 التفريغ:\n{transcription}\n\n💬 الرد:\n{reply}"

async def _process_video(url: str, prompt: str, messages: list) -> str:
    try:
        import subprocess, tempfile, os as _os
        raw, _ = await _fetch_base64(url)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(raw)
            vid_path = f.name
        frame_path = vid_path.replace(".mp4", "_frame.jpg")
        proc = subprocess.run(
            ["ffmpeg", "-i", vid_path, "-ss", "00:00:01", "-vframes", "1", "-q:v", "2", frame_path, "-y"],
            capture_output=True, timeout=30
        )
        _os.unlink(vid_path)
        if proc.returncode != 0 or not _os.path.exists(frame_path):
            raise RuntimeError("ffmpeg failed")
        with open(frame_path, "rb") as f:
            frame_raw = f.read()
        _os.unlink(frame_path)
        reply = await _groq_vision(messages, base64.b64encode(frame_raw).decode(), "image/jpeg")
        return f"🎬 تحليل الفيديو (الإطار الأول):\n{reply}"
    except Exception as e:
        return f"⚠️ تعذّر تحليل الفيديو ({str(e)[:60]}). يمكنك أخذ screenshot وإرساله كصورة."

async def _gemini_fallback(messages: list) -> str:
    contents = []
    for m in messages:
        if m["role"] == "system": continue
        role = "model" if m["role"] == "assistant" else "user"
        content = m["content"] if isinstance(m["content"], str) else str(m.get("content", ""))
        contents.append({"role": role, "parts": [{"text": content}]})
    for key in GEMINI_KEYS:
        try:
            r = await _http.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
                headers={"Content-Type": "application/json", "X-goog-api-key": key},
                json={
                    "systemInstruction": {"parts": [{"text": SYSTEM}]},
                    "contents": contents,
                    "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024},
                },
                timeout=25,
            )
            if r.status_code == 429: continue
            r.raise_for_status()
            reply = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            if reply: return reply
        except Exception:
            continue
    raise RuntimeError("ALL_GEMINI_EXHAUSTED")


def register(app):

    @app.post("/groq")
    async def groq_endpoint(request: Request):
        """
        Body:
          نمط الجلسات (جديد):
            { "thread_id": "group_123", "sender_name": "Ahmed", "prompt": "...", "clear": false,
              "attachment": { "kind": "image|audio|video", "url": "...", "base64": "...", "contentType": "..." } }
          النمط القديم (messages مباشرة):
            { "messages": [...], "attachment": {...} }
        """
        try:
            body = await request.json()

            # ─── نمط الجلسات (thread_id / prompt) ────────────
            if "thread_id" in body or "prompt" in body:
                thread_id   = body.get("thread_id", "default")
                sender_name = body.get("sender_name", "مستخدم")
                prompt      = body.get("prompt", "").strip()
                do_clear    = body.get("clear", False)
                attachment  = body.get("attachment")

                if do_clear:
                    await _clear(thread_id)
                    return JSONResponse({"reply": "🧹 تم مسح ذاكرة المجموعة."})

                ctx = await _load(thread_id)
                user_content = f"[{sender_name}]: {prompt}" if prompt else f"[{sender_name}]: ما هذا؟"

                messages = [
                    {"role": "system", "content": SYSTEM},
                    *ctx,
                    {"role": "user",   "content": user_content},
                ]

                try:
                    kind = attachment.get("kind") if attachment else None
                    att_url = attachment.get("url") if attachment else None

                    if kind == "image":
                        b64  = attachment.get("base64")
                        mime = attachment.get("contentType", "image/jpeg")
                        if not b64 and att_url:
                            raw, b64 = await _fetch_base64(att_url)
                            mime = _guess_mime(att_url, raw)
                        reply = await _groq_vision(messages, b64, mime)
                        provider = "groq-vision"
                    elif kind == "audio" and att_url:
                        raw, _ = await _fetch_base64(att_url)
                        mime = _guess_mime(att_url, raw)
                        reply = await _groq_audio(raw, mime, prompt)
                        provider = "groq-whisper"
                    elif kind == "video" and att_url:
                        reply = await _process_video(att_url, prompt, messages)
                        provider = "groq-video"
                    else:
                        reply = await _groq_text(messages)
                        provider = "groq"
                except Exception:
                    try:
                        reply = await _gemini_fallback(messages)
                        provider = "gemini-fallback"
                    except Exception as e2:
                        return JSONResponse({"error": f"كل الخوادم فشلت: {str(e2)[:100]}"}, status_code=503)

                # خزّن النص فقط في السياق (لا الصور)
                att_label = f"[{attachment.get('kind', '')}] " if attachment else ""
                user_text = f"[{sender_name}]: {att_label}{prompt}".strip()
                await _save(thread_id, [
                    *ctx,
                    {"role": "user",      "content": user_text},
                    {"role": "assistant", "content": reply},
                ])
                return JSONResponse({"reply": reply, "provider": provider})

            # ─── النمط القديم: messages مباشرة ───────────────
            messages = body.get("messages", [])
            if not messages:
                return JSONResponse({"error": "messages أو prompt مطلوب"}, status_code=400)

            if not any(m.get("role") == "system" for m in messages):
                messages = [{"role": "system", "content": SYSTEM}] + messages

            last = messages[-1]
            attachment = last.pop("attachment", None)
            kind    = attachment.get("kind")    if attachment else None
            att_url = attachment.get("url")     if attachment else None
            prompt  = last.get("content", "")  if isinstance(last.get("content"), str) else ""

            try:
                if kind == "image":
                    b64  = attachment.get("base64")
                    mime = attachment.get("contentType", "image/jpeg")
                    if not b64 and att_url:
                        raw, b64 = await _fetch_base64(att_url)
                        mime = _guess_mime(att_url, raw)
                    reply = await _groq_vision(messages, b64, mime)
                    return JSONResponse({"reply": reply, "provider": "groq-vision"})
                elif kind == "audio" and att_url:
                    raw, _ = await _fetch_base64(att_url)
                    mime = _guess_mime(att_url, raw)
                    reply = await _groq_audio(raw, mime, prompt)
                    return JSONResponse({"reply": reply, "provider": "groq-whisper"})
                elif kind == "video" and att_url:
                    reply = await _process_video(att_url, prompt, messages)
                    return JSONResponse({"reply": reply, "provider": "groq-video"})
                else:
                    try:
                        reply = await _groq_text(messages)
                        return JSONResponse({"reply": reply, "provider": "groq"})
                    except Exception:
                        reply = await _gemini_fallback(messages)
                        return JSONResponse({"reply": reply, "provider": "gemini"})
            except Exception:
                try:
                    reply = await _gemini_fallback(messages)
                    return JSONResponse({"reply": reply, "provider": "gemini-fallback"})
                except Exception as e2:
                    return JSONResponse({"error": f"كل الخوادم فشلت: {str(e2)[:100]}"}, status_code=503)

        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)
