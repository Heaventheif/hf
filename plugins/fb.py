"""
plugins/fb.py
endpoint: POST /fb
"""
import httpx, base64, re, subprocess, tempfile, os
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool

DESCRIPTION = "تحميل فيديوهات فيسبوك"

# ─── Shared HTTP clients (connection pooling) ──────────────────
_http = httpx.AsyncClient(
    timeout=30,
    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
)
_http_dl = httpx.AsyncClient(
    timeout=120,
    follow_redirects=True,
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)

FDOWN    = "https://facebook-video-download-api.onrender.com"
MAX_BYTES = 25 * 1024 * 1024

# ─── فلترة الروابط: فيديوهات/ريلز فقط، رفض المنشورات/الصور/البروفايلات ──────
# أنماط مقبولة (فيديو حقيقي):
#   facebook.com/watch/?v=...   |  facebook.com/watch?v=...
#   facebook.com/reel/<id>      |  facebook.com/reels/<id>
#   facebook.com/<page>/videos/<id>
#   fb.watch/<code>             |  fbwat.ch/<code>
#   m.facebook.com (نفس الأنماط أعلاه)
_VIDEO_URL_PATTERNS = [
    r"facebook\.com/(?:[\w.\-]+/)?watch/?(?:\?|$|/)",          # /watch or /watch?v=
    r"facebook\.com/reels?/\d+",                                # /reel/123 or /reels/123
    r"facebook\.com/[\w.\-]+/videos/\d+",                       # /<page>/videos/123
    r"facebook\.com/video\.php",                                # legacy /video.php?v=
    r"(?:^|//)fb\.watch/[\w-]+",
    r"(?:^|//)fbwat\.ch/[\w-]+",
]

# أنماط مرفوضة بشكل صريح حتى لو طابقت جزئياً نمطاً أعلاه (منشورات/صور/بروفايل)
_NON_VIDEO_URL_PATTERNS = [
    r"facebook\.com/[\w.\-]+/posts/",
    r"facebook\.com/photo",
    r"facebook\.com/[\w.\-]+/photos/",
    r"facebook\.com/groups/[\w.\-]+/(?:posts/|permalink/)",
    r"facebook\.com/story\.php",
    r"facebook\.com/marketplace/",
    r"facebook\.com/events/",
]


_ALLOWED_HOSTS_SUFFIXES = ("facebook.com", "fb.watch", "fbwat.ch")


def _is_facebook_video_url(url: str) -> bool:
    """يقبل فقط روابط الفيديوهات/الريلز، ويرفض المنشورات والصور والبروفايلات."""
    if not url:
        return False

    from urllib.parse import urlparse
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False

    host = (parsed.hostname or "").lower()
    if not host:
        return False

    # يجب أن يكون الدومين facebook.com أو fb.watch أو fbwat.ch (أو أحد فروعها الفرعية m./www. إلخ)
    # وليس دومين مزوّر مثل facebook.com.evil.com
    if not any(host == d or host.endswith("." + d) for d in _ALLOWED_HOSTS_SUFFIXES):
        return False

    low = url.strip().lower()

    for pat in _NON_VIDEO_URL_PATTERNS:
        if re.search(pat, low):
            return False

    for pat in _VIDEO_URL_PATTERNS:
        if re.search(pat, low):
            return True

    return False


async def _get_video_candidates(fb_url: str, quality: str) -> dict:
    """يرجّع كل روابط الفيديو المرشّحة (مش رابط واحد بس) بترتيب الأولوية:
    download_url أولاً، ثم كل available_formats. بعض هالصيغ (خصوصاً جودة
    "worst"/المنخفضة) تكون فيديو بدون صوت من خدمة التحميل نفسها — فبدل
    الاكتفاء بأول رابط، نجرّبهم بالترتيب لحد ما نلاقي واحد فيه صوت فعلاً.
    """
    r = await _http.post(
        f"{FDOWN}/download",
        json={"url": fb_url, "quality": quality},
        headers={"Content-Type": "application/json"},
    )
    r.raise_for_status()
    data = r.json()

    urls: list[str] = []
    if data.get("download_url"):
        urls.append(data["download_url"])
    for fmt in (data.get("available_formats") or []):
        u = fmt.get("url")
        if u and u not in urls:
            urls.append(u)

    return {
        "video_urls": urls,
        "title": data.get("video_info", {}).get("title", "فيديو فيسبوك"),
    }


def _has_audio_stream_sync(file_path: str) -> bool:
    """يتحقق (عبر ffprobe) هل الملف فيه مسار صوت فعلي أم لا."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", file_path],
            capture_output=True, timeout=15, text=True,
        )
        return bool(proc.stdout.strip())
    except Exception:
        # لو ffprobe نفسه فشل (غير مثبت مثلاً)، ما نمنع الإرسال — نفترض OK
        # بدل ما نكسر الميزة بالكامل بسبب فحص إضافي فشل.
        return True


async def _has_audio_stream(content: bytes) -> bool:
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(content)
        path = f.name
    try:
        return await run_in_threadpool(_has_audio_stream_sync, path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def _download_with_audio(fb_url: str, quality: str) -> "tuple[bytes, str] | None":
    """يجرّب كل الروابط المرشّحة لجودة معيّنة، ويرجع أول واحد ينزّل بنجاح
    *وفيه صوت فعلاً*. يرجع None لو ولا واحد نجح/فيه صوت."""
    try:
        cand = await _get_video_candidates(fb_url, quality)
    except Exception:
        return None

    title = cand["title"]
    fallback_silent: "tuple[bytes, str] | None" = None  # آخر ملجأ لو ولا فيديو فيه صوت

    for video_url in cand["video_urls"]:
        try:
            dl = await _http_dl.get(video_url)
            dl.raise_for_status()
            content = dl.content
        except Exception:
            continue
        if not content or len(content) > MAX_BYTES:
            continue

        if await _has_audio_stream(content):
            return content, title
        elif fallback_silent is None:
            fallback_silent = (content, title)  # نحتفظ فيه احتياط لو كل الخيارات بدون صوت

    return fallback_silent


def register(app):

    @app.post("/fb")
    async def fb_download(request: Request):
        """
        Body: { "url": "https://facebook.com/...", "quality": "worst" | "720p" }
        Response:
          مع ملف:    { "video_b64": "...", "title": "...", "size": N }
          برابط:     { "video_url": "...", "title": "..." }
        """
        try:
            body    = await request.json()
            fb_url  = body.get("url", "").strip()
            quality = body.get("quality", "worst")

            if not fb_url:
                return JSONResponse({"error": "url مطلوب"}, status_code=400)

            if not _is_facebook_video_url(fb_url):
                return JSONResponse({
                    "error": "الرابط ليس فيديو/ريل فيسبوك صالحاً. الأنواع المدعومة: "
                             "facebook.com/watch?v=... ، facebook.com/reel/... ، "
                             "facebook.com/<page>/videos/... ، fb.watch/... "
                             "(لا يدعم المنشورات أو الصور أو روابط البروفايل)"
                }, status_code=400)

            # جرب الجودة المطلوبة ثم worst كـ fallback — كل واحدة تجرّب
            # كل الروابط المرشّحة وتتحقق من وجود صوت فعلي (ffprobe) قبل
            # القبول، بدل الاكتفاء بأول رابط ترجعه الخدمة الخارجية.
            qualities  = [quality, "worst"] if quality != "worst" else ["worst"]
            content    = None
            title      = None
            for q in qualities:
                result = await _download_with_audio(fb_url, q)
                if result:
                    content, title = result
                    break

            if not content:
                return JSONResponse({"error": "لم يُعثر على الفيديو"}, status_code=404)

            return JSONResponse({
                "video_b64": base64.b64encode(content).decode(),
                "title":     title,
                "size":      len(content),
            })

        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)
