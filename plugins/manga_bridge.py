"""
plugins/manga_bridge.py
────────────────────────────────────────────────────────────────
نسخة "كل حاجة في ملف واحد، من غير worker خارجي ومن غير بروكسي":
الكشط بقى بيحصل هنا مباشرة جوه نفس الـ HF Space باستخدام Playwright
(المتصفح مثبّت أصلاً في Dockerfile.txt بتاعك: playwright install
chromium + install-deps).

⚠️ ملحوظة صريحة قبل ما تجرّب: الـ Space لسه شغال من IP داتا سنتر،
وCloudflare (زي ما شفنا في اللوق: status=403, server=cloudflare,
"Just a moment...") بيقيّم الطلب غالبًا بناءً على سمعة الـ IP نفسه
مش بس على "هل فيه متصفح حقيقي ولا لأ". يعني فيه احتمال حقيقي إن
Playwright من هنا يفشل برضه بنفس الطريقة، حتى لو محلي بيحل تحدي
جافاسكريبت المتصفح فعليًا. بنجربها لأن التكلفة صفر (Playwright
مثبّت أصلاً وملوش أي متطلبات إضافية) — لكن لو فشلت هنا كمان، يبقى
مؤكد 100% إن السبب هو حظر IP الداتا سنتر نفسه، ومفيش حل غير بروكسي
residential أو جهاز/استضافة بـ IP نظيف.

الـ API متطابق تمامًا مع النسخة اللي كان فيها worker خارجي، فـ
manga2.js مش محتاج أي تعديل:
  POST /manga-bridge/jobs                → ينشئ job وبيبدأ الكشط فورًا في الخلفية
  GET  /manga-bridge/jobs/{id}           → حالة الـ job (pending/running/done/error)
  GET  /manga-bridge/jobs/{id}/image/{i} → صورة واحدة بالترتيب

الحماية: محمي تلقائيًا بنفس middleware التوكن السري (X-Internal-Token)
المُفعّل في plugin_loader.py.

التخزين: SQLite (قياسية) للميتاداتا + ملفات على القرص للصور.
"""

import os
import sqlite3
import time
import uuid
import threading
import logging

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse

logger = logging.getLogger("manga_bridge")

DESCRIPTION = "كشط مباشر بدون worker خارجي وبدون بروكسي (Playwright جوه نفس الـ Space)"

DATA_DIR   = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "manga_bridge")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
DB_PATH    = os.path.join(DATA_DIR, "jobs.db")
os.makedirs(IMAGES_DIR, exist_ok=True)

BASE_URL = "https://3asq.pro"
CF_CHALLENGE_MAX_WAIT = 20   # ثانية — أقصى انتظار لحل تحدي Cloudflare
JOB_TTL_SECONDS = 60 * 60    # ساعة — أي job أقدم من كده تتنضف تلقائيًا

_lock = threading.Lock()


# ─── تخزين (SQLite) ────────────────────────────────────────────

def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _init_db():
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            manga TEXT,
            chapter TEXT,
            status TEXT,           -- pending | running | done | error
            chapter_title TEXT,
            chapter_url TEXT,
            image_count INTEGER DEFAULT 0,
            error TEXT,
            created_at REAL,
            updated_at REAL
        )
    """)
    conn.commit()
    conn.close()


_init_db()


def _update_job(job_id, **fields):
    fields["updated_at"] = time.time()
    keys = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [job_id]
    with _lock:
        conn = _get_conn()
        conn.execute(f"UPDATE jobs SET {keys} WHERE id=?", values)
        conn.commit()
        conn.close()


def _cleanup_old_jobs():
    """يمسح jobs وصورها الأقدم من JOB_TTL_SECONDS — بنستدعيها مع كل
    job جديدة بدل عمل scheduler منفصل."""
    cutoff = time.time() - JOB_TTL_SECONDS
    with _lock:
        conn = _get_conn()
        rows = conn.execute("SELECT id FROM jobs WHERE created_at < ?", (cutoff,)).fetchall()
        for (job_id,) in rows:
            job_dir = os.path.join(IMAGES_DIR, job_id)
            if os.path.isdir(job_dir):
                for f in os.listdir(job_dir):
                    try:
                        os.remove(os.path.join(job_dir, f))
                    except OSError:
                        pass
                try:
                    os.rmdir(job_dir)
                except OSError:
                    pass
        conn.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))
        conn.commit()
        conn.close()


# ─── الكشط الفعلي (Playwright، sync) ───────────────────────────

def slugify(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def _wait_for_cloudflare(page):
    for _ in range(CF_CHALLENGE_MAX_WAIT):
        try:
            if "Just a moment" not in page.title():
                return True
        except Exception:
            pass
        time.sleep(1)
    try:
        return "Just a moment" not in page.title()
    except Exception:
        return False


def _scrape_chapter_sync(manga: str, chapter: str):
    """يشتغل جوه threadpool (Playwright sync API مش متوافقة مع asyncio
    مباشرة). يرجع (chapter_title, chapter_url, [(idx, bytes), ...])."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        try:
            slug = slugify(manga)
            url = f"{BASE_URL}/manga/{slug}/{chapter}/"
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            _wait_for_cloudflare(page)

            imgs = page.query_selector_all(".page-break img, .reading-content img")

            # لو مفيش صور، جرّب البحث (زي منطق manga2.js الأصلي)
            if not imgs:
                page.goto(f"{BASE_URL}/?s={manga}", wait_until="domcontentloaded", timeout=30000)
                _wait_for_cloudflare(page)
                first = page.query_selector(".page-item-detail .item-thumb a")
                if not first:
                    return None, None, []
                href = first.get_attribute("href")
                if not href:
                    return None, None, []
                page.goto(f"{href.rstrip('/')}/{chapter}/", wait_until="domcontentloaded", timeout=45000)
                _wait_for_cloudflare(page)
                imgs = page.query_selector_all(".page-break img, .reading-content img")

            if not imgs:
                return page.title(), page.url, []

            srcs = []
            for img in imgs:
                src = (
                    img.get_attribute("data-src")
                    or img.get_attribute("data-lazy-src")
                    or img.get_attribute("src")
                )
                if src:
                    srcs.append(src)

            chapter_title, chapter_url = page.title(), page.url

            logger.info(f"[manga-bridge] {len(srcs)} صورة موجودة في الصفحة، جاري التحميل...")

            # مهم: بنحتفظ بس بالصور اللي نجحت، وبنرقّمها من جديد بالترتيب
            # المتتابع (0, 1, 2...) — لو حافظنا على الترقيم الأصلي وفيه صور
            # فشلت في النص، هيبقى فيه فجوات (مثلاً 0,1,4,5...) بينما
            # image_count المُرسَل هيفضل بيقول العدد الكلي الأصلي، فالبوت
            # هيحاول يحمّل index مش موجود ويرجعله 404 لصور تانية سليمة أصلاً.
            files = []
            for original_i, src in enumerate(srcs):
                resp = None
                for attempt in range(2):  # محاولة + إعادة محاولة واحدة
                    try:
                        resp = context.request.get(src, headers={"Referer": chapter_url}, timeout=20000)
                        if resp.ok:
                            break
                        if attempt == 0:
                            time.sleep(1)
                    except Exception as e:
                        resp = None
                        if attempt == 0:
                            time.sleep(1)
                        else:
                            logger.warning(f"فشل تحميل صورة (ترتيبها الأصلي {original_i}): {e} — {src}")

                if resp is not None and resp.ok:
                    files.append((len(files), resp.body()))
                elif resp is not None:
                    logger.warning(f"فشل تحميل صورة (ترتيبها الأصلي {original_i}): HTTP {resp.status} — {src}")

            return chapter_title, chapter_url, files
        finally:
            browser.close()


def _run_job(job_id: str, manga: str, chapter: str):
    """بتشتغل كـ FastAPI BackgroundTask (بعد إرسال رد /jobs مباشرة).
    Starlette بيشغّلها في threadpool تلقائيًا لأنها sync function."""
    _update_job(job_id, status="running")
    try:
        chapter_title, chapter_url, files = _scrape_chapter_sync(manga, chapter)

        if not files:
            _update_job(
                job_id, status="error",
                error="لم يتم العثور على صور — إما اسم/فصل غير صحيح، أو تحدي "
                      "Cloudflare لم يُحل من IP الـ Space (راجع اللوق للتفاصيل)."
            )
            return

        job_dir = os.path.join(IMAGES_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        for idx, content in files:
            with open(os.path.join(job_dir, f"{idx}.jpg"), "wb") as f:
                f.write(content)

        _update_job(
            job_id, status="done",
            chapter_title=chapter_title or "", chapter_url=chapter_url or "",
            image_count=len(files),
        )
        logger.info(f"[manga-bridge] job {job_id} اكتملت — {len(files)} صورة")

    except Exception as e:
        logger.exception(f"[manga-bridge] job {job_id} فشلت")
        _update_job(job_id, status="error", error=str(e)[:500])


# ─── الـ endpoints ──────────────────────────────────────────────

router = APIRouter(prefix="/manga-bridge", tags=["manga-bridge"])


@router.post("/jobs")
def create_job(payload: dict, background_tasks: BackgroundTasks):
    """
    body: {"manga": "dr stone", "chapter": "221"}
    (أي حقول زيادة زي "source" من نسخة قديمة بتتجاهل تلقائيًا)
    """
    _cleanup_old_jobs()

    manga = (payload or {}).get("manga", "").strip()
    chapter = (payload or {}).get("chapter", "").strip()
    if not manga or not chapter:
        raise HTTPException(400, "الحقول manga و chapter مطلوبة")

    job_id = uuid.uuid4().hex[:16]
    now = time.time()

    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO jobs (id, manga, chapter, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (job_id, manga, chapter, now, now),
        )
        conn.commit()
        conn.close()

    logger.info(f"[manga-bridge] job جديدة {job_id} | {manga} #{chapter}")
    background_tasks.add_task(_run_job, job_id, manga, chapter)
    return {"job_id": job_id, "status": "pending"}


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    conn = _get_conn()
    row = conn.execute(
        "SELECT status, chapter_title, chapter_url, image_count, error FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "job غير موجودة (ممكن تكون انتهت صلاحيتها)")
    status, chapter_title, chapter_url, image_count, error = row
    return {
        "status": status,
        "chapter_title": chapter_title,
        "chapter_url": chapter_url,
        "image_count": image_count,
        "error": error,
    }


@router.get("/jobs/{job_id}/image/{idx}")
def get_job_image(job_id: str, idx: int):
    file_path = os.path.join(IMAGES_DIR, job_id, f"{idx}.jpg")
    if not os.path.exists(file_path):
        raise HTTPException(404, "الصورة غير موجودة")
    return FileResponse(file_path)


def register(app):
    app.include_router(router)
