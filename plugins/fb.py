"""
plugins/fb.py
endpoint: POST /fb
"""
import httpx, base64, re
from fastapi import Request
from fastapi.responses import JSONResponse

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


async def _get_video_url(fb_url: str, quality: str) -> dict:
    r = await _http.post(
        f"{FDOWN}/download",
        json={"url": fb_url, "quality": quality},
        headers={"Content-Type": "application/json"},
    )
    r.raise_for_status()
    data = r.json()
    return {
        "video_url": data.get("download_url") or (data.get("available_formats") or [{}])[0].get("url"),
        "title":     data.get("video_info", {}).get("title", "فيديو فيسبوك"),
    }


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

            # جرب الجودة المطلوبة ثم worst كـ fallback
            qualities  = [quality, "worst"] if quality != "worst" else ["worst"]
            result     = None
            last_error = None
            for q in qualities:
                try:
                    r = await _get_video_url(fb_url, q)
                    if r["video_url"]:
                        result = r
                        break
                except Exception as e:
                    last_error = e
                    continue

            if not result or not result["video_url"]:
                if last_error is not None:
                    # فشل من جهة الخدمة الخارجية (خطأ شبكة/استجابة غير متوقعة) — ليس 404 حقيقياً
                    return JSONResponse({
                        "error": f"فشل الاتصال بخدمة التحميل: {str(last_error)[:200]}"
                    }, status_code=502)
                return JSONResponse({"error": "لم يُعثر على الفيديو"}, status_code=404)

            video_url = result["video_url"]
            title     = result["title"]

            # حاول تحميل الفيديو
            dl = await _http_dl.get(video_url)
            dl.raise_for_status()
            content = dl.content

            if not content:
                return JSONResponse({"error": "الملف فارغ"}, status_code=502)

            if len(content) > MAX_BYTES:
                # حاول بجودة أقل إذا لم نكن عليها
                if quality != "worst":
                    try:
                        r2 = await _get_video_url(fb_url, "worst")
                        if r2["video_url"]:
                            dl2 = await _http_dl.get(r2["video_url"])
                            content2 = dl2.content
                            if content2 and len(content2) <= MAX_BYTES:
                                return JSONResponse({
                                    "video_b64": base64.b64encode(content2).decode(),
                                    "title":     title,
                                    "size":      len(content2),
                                })
                    except Exception:
                        pass
                return JSONResponse({"error": "الفيديو أكبر من 25MB"}, status_code=413)

            return JSONResponse({
                "video_b64": base64.b64encode(content).decode(),
                "title":     title,
                "size":      len(content),
            })

        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)
