"""
plugins/manga_bridge.py
────────────────────────────────────────────────────────────────
جسر (bridge) بين البوت (Node.js) وحاوية كشط خارجية عندها IP نظيف
قادر يعدّي تحدي Cloudflare (زي 3asq.pro). الـ backend ده (سواء شغال
على HF Space أو Render) مايكشطش بنفسه أبداً — هو بس "صندوق بريد"
مشترك بين طرفين:

  1) البوت يفتح job جديدة:      POST /manga-bridge/jobs
  2) الحاوية بتعمل poll دوري:   GET  /manga-bridge/jobs/next
     (long-poll — بترجع فورًا لو فيه job، أو بعد wait_seconds لو مفيش)
  3) الحاوية ترفع النتيجة:      POST /manga-bridge/jobs/{id}/complete
  4) البوت يتابع الحالة:        GET  /manga-bridge/jobs/{id}
  5) البوت يحمّل كل صورة:       GET  /manga-bridge/jobs/{id}/image/{i}

اخترنا نمط "poll من الحاوية" مش "نداء مباشر من الجسر للحاوية" عشان
يشتغل حتى لو الحاوية خلف NAT ومفيهاش IP عام تستقبل عليه طلبات —
وهو برضه شغال عادي لو كان عندها IP عام (بس أقل كفاءة شوية من نداء
مباشر، وده تنازل بسيط مقابل إنه يشتغل في كل الحالات).

الحماية: كل الـ endpoints هنا محمية تلقائيًا بنفس middleware التوكن
السري (X-Internal-Token) المُفعّل بالفعل في plugin_loader.py — مفيش
حاجة إضافية مطلوبة هنا، بس تأكد إن INTERNAL_TOKEN مضبوط في متغيرات
البيئة، وإن كل من البوت والحاوية بيبعتوا نفس الهيدر.

التخزين: SQLite (مكتبة قياسية، بدون أي متطلبات جديدة) للميتاداتا +
ملفات على القرص للصور نفسها (أخف وأسرع من base64 داخل الداتابيز).
المسار مشترك بين workers الـ uvicorn (نفس الحاوية/القرص)، والقفل
(threading.Lock) + وضع WAL في SQLite كافيين لحجم استخدام بوت شخصي.

ملاحظة: الـ jobs بتتنضف تلقائيًا بعد JOB_TTL_SECONDS من إنشائها.
"""

import os
import sqlite3
import time
import uuid
import threading
import logging

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse

logger = logging.getLogger("manga_bridge")

DESCRIPTION = "جسر بين البوت وحاوية كشط خارجية (لتخطي حظر Cloudflare) عبر طابور jobs"

DATA_DIR   = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "manga_bridge")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
DB_PATH    = os.path.join(DATA_DIR, "jobs.db")

os.makedirs(IMAGES_DIR, exist_ok=True)

JOB_TTL_SECONDS = 60 * 60  # ساعة — أي job أقدم من كده تتنضف تلقائيًا مع كل طلب جديد

_lock = threading.Lock()  # حماية إضافية داخل نفس الـ process (فوق حماية WAL نفسها)


def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _init_db():
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            source TEXT,
            manga TEXT,
            chapter TEXT,
            status TEXT,          -- pending | in_progress | done | error
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


def _cleanup_old_jobs():
    """يمسح jobs وصورها الأقدم من JOB_TTL_SECONDS. بنستدعيها مع كل job
    جديدة بدل عمل scheduler منفصل — كافي لحجم استخدام بوت شخصي."""
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


router = APIRouter(prefix="/manga-bridge", tags=["manga-bridge"])


# ─── 1) البوت: فتح job جديدة ──────────────────────────────────

@router.post("/jobs")
def create_job(payload: dict):
    """
    body: {"source": "3asq", "manga": "dr stone", "chapter": "221"}
    source اختياري (افتراضي "default") — بيسمح لأكتر من حاوية/موقع
    مستقبلاً إن كل واحدة تفلتر الـ jobs اللي تخصها فقط.
    """
    _cleanup_old_jobs()

    manga = (payload or {}).get("manga", "").strip()
    chapter = (payload or {}).get("chapter", "").strip()
    source = (payload or {}).get("source", "default").strip() or "default"

    if not manga or not chapter:
        raise HTTPException(400, "الحقول manga و chapter مطلوبة")

    job_id = uuid.uuid4().hex[:16]
    now = time.time()

    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO jobs (id, source, manga, chapter, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (job_id, source, manga, chapter, now, now),
        )
        conn.commit()
        conn.close()

    logger.info(f"[manga-bridge] job جديدة {job_id} | {source} | {manga} #{chapter}")
    return {"job_id": job_id, "status": "pending"}


# ─── 2) الحاوية: طلب أقدم job معلّقة (long-poll) ──────────────

@router.get("/jobs/next")
def next_job(source: str = "default", wait_seconds: int = 25):
    """
    long-poll بسيط: بيفحص كل ثانية لحد ما يلاقي job معلّقة أو تنتهي
    مهلة wait_seconds — بيقلل عدد طلبات الـ poll الفاضية من الحاوية
    مقارنة بـ poll عادي كل ثانية بدون انتظار.
    """
    deadline = time.time() + max(0, min(wait_seconds, 55))

    while True:
        row = None
        with _lock:
            conn = _get_conn()
            row = conn.execute(
                "SELECT id, manga, chapter FROM jobs "
                "WHERE status='pending' AND source=? ORDER BY created_at ASC LIMIT 1",
                (source,),
            ).fetchone()
            if row:
                job_id = row[0]
                conn.execute(
                    "UPDATE jobs SET status='in_progress', updated_at=? WHERE id=?",
                    (time.time(), job_id),
                )
                conn.commit()
            conn.close()

        if row:
            return {"job_id": row[0], "manga": row[1], "chapter": row[2]}

        if time.time() >= deadline:
            return JSONResponse(status_code=204, content=None)
        time.sleep(1)


# ─── 3) الحاوية: رفع نتيجة الكشط ───────────────────────────────

@router.post("/jobs/{job_id}/complete")
async def complete_job(
    job_id: str,
    chapter_title: str = Form(""),
    chapter_url: str = Form(""),
    error: str = Form(""),
    images: list[UploadFile] = File(default=[]),
):
    """
    multipart/form-data:
      - chapter_title, chapter_url: اختياري، معلومات وصفية فقط
      - error: لو الكشط فشل من عند الحاوية (مثلاً تحدي Cloudflare
        ماتحلّش، أو اسم/فصل غلط) — لو موجود بنتجاهل images تمامًا
      - images: ملفات الصور، الترتيب اللي بتوصل بيه هو اللي بيتحفظ
        (استخدم اسم ملف زي "0.jpg", "1.jpg"... أو أي اسم، المهم الترتيب)
    """
    with _lock:
        conn = _get_conn()
        exists = conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
    if not exists:
        raise HTTPException(404, "job غير موجودة (ممكن تكون انتهت صلاحيتها)")

    if error:
        with _lock:
            conn = _get_conn()
            conn.execute(
                "UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=?",
                (error[:500], time.time(), job_id),
            )
            conn.commit()
            conn.close()
        logger.warning(f"[manga-bridge] job {job_id} فشلت من الحاوية: {error[:200]}")
        return {"status": "error"}

    job_dir = os.path.join(IMAGES_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    saved = 0
    for idx, upload in enumerate(images):
        content = await upload.read()
        if not content:
            continue
        ext = os.path.splitext(upload.filename or "")[1] or ".jpg"
        with open(os.path.join(job_dir, f"{idx}{ext}"), "wb") as f:
            f.write(content)
        saved += 1

    if saved == 0:
        with _lock:
            conn = _get_conn()
            conn.execute(
                "UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=?",
                ("لم تصل أي صورة صالحة من الحاوية", time.time(), job_id),
            )
            conn.commit()
            conn.close()
        return {"status": "error"}

    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE jobs SET status='done', chapter_title=?, chapter_url=?, "
            "image_count=?, updated_at=? WHERE id=?",
            (chapter_title, chapter_url, saved, time.time(), job_id),
        )
        conn.commit()
        conn.close()

    logger.info(f"[manga-bridge] job {job_id} اكتملت — {saved} صورة")
    return {"status": "done", "image_count": saved}


# ─── 4) البوت: فحص حالة job ────────────────────────────────────

@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    conn = _get_conn()
    row = conn.execute(
        "SELECT status, chapter_title, chapter_url, image_count, error "
        "FROM jobs WHERE id=?",
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


# ─── 5) البوت: تحميل صورة واحدة بالترتيب ──────────────────────

@router.get("/jobs/{job_id}/image/{idx}")
def get_job_image(job_id: str, idx: int):
    job_dir = os.path.join(IMAGES_DIR, job_id)
    if not os.path.isdir(job_dir):
        raise HTTPException(404, "لا توجد صور لهذه الـ job")
    for f in os.listdir(job_dir):
        if f.startswith(f"{idx}."):
            return FileResponse(os.path.join(job_dir, f))
    raise HTTPException(404, "الصورة غير موجودة")


def register(app):
    app.include_router(router)
