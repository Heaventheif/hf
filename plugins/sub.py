import os
import re
import uuid
import httpx
import subprocess
from fastapi import BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

# معلومات وحزم النظام المطلوبة للـ plugin
DESCRIPTION = "إضافة ترجمة نصية ثابتة أو زمنية SRT على مقاطع الفيديو القصيرة"
# fonts-noto-core لا يكفي للعربي (مش فيه Naskh/Sans Arabic) + libass محتاج fontconfig
DOCKERFILE_DEPS = ["ffmpeg", "fonts-noto-core", "fonts-noto-ui-core", "fontconfig"]

# اسم عائلة الخط العربي المستخدم في الحرق (يجب أن يكون مثبتاً فعلياً في الـ Docker)
ARABIC_FONT_FAMILY = "Noto Naskh Arabic"

# ذاكرة مؤقتة لمتابعة حالة معالجة الفيديوهات في الخلفية
_sub_jobs = {}


class SubtitleRequest(BaseModel):
    video_url: str = Field(..., description="رابط تحميل الفيديو المباشر من مسنجر/ريندر")
    sub_text: str = Field(..., min_length=1, description="نص الترجمة الثابت أو التنسيق الزمني")


def _srt_escape(text: str) -> str:
    """تنظيف بسيط للنص من أي أسطر فاضية أو محارف قد تكسر تحليل الـ SRT."""
    return text.replace("\r", "").strip()


def convert_to_srt(user_text: str) -> str:
    """تحويل النص المرقم زمنياً من المستخدم إلى صيغة SRT القياسية"""
    lines = [line.strip() for line in user_text.strip().split("\n") if line.strip()]
    srt_output = ""
    index = 1

    for line in lines:
        match = re.match(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\s*\|\s*(.*)", line)
        if match:
            start_min_sec = match.group(1)
            end_min_sec = match.group(2)
            text_content = _srt_escape(match.group(3))

            start_time = f"00:{start_min_sec},000"
            end_time = f"00:{end_min_sec},000"

            srt_output += f"{index}\n{start_time} --> {end_time}\n{text_content}\n\n"
            index += 1

    return srt_output


def _get_video_duration_srt_timestamp(video_path: str) -> str:
    """يجلب مدة الفيديو الحقيقية عبر ffprobe ويحولها لصيغة توقيت SRT (HH:MM:SS,mmm)."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    duration_seconds = float(result.stdout.decode().strip())
    # هامش أمان بسيط لضمان بقاء الترجمة ظاهرة حتى آخر إطار
    duration_seconds = max(duration_seconds - 0.05, 0.1)

    hours = int(duration_seconds // 3600)
    minutes = int((duration_seconds % 3600) // 60)
    seconds = int(duration_seconds % 60)
    millis = int(round((duration_seconds - int(duration_seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _build_static_srt(video_path: str, sub_text: str) -> str:
    """يبني ملف SRT بسطر واحد يغطي الفيديو بالكامل، لاستخدام نفس فلتر subtitles (libass)
    بدل drawtext، وبالتالي حل مشكلة الخط وتشكيل الحروف العربية دفعة واحدة."""
    end_ts = _get_video_duration_srt_timestamp(video_path)
    text = _srt_escape(sub_text)
    return f"1\n00:00:00,000 --> {end_ts}\n{text}\n\n"


def _process_video_subtitles(job_id: str, video_url: str, sub_text: str):
    """المعالجة الثقيلة للفيديو في الخلفية داخل الـ Threadpool الخاص بـ FastAPI"""
    unique_id = str(uuid.uuid4())[:8]
    input_video = f"input_{unique_id}.mp4"
    output_video = f"output_{unique_id}.mp4"
    srt_file = f"sub_{unique_id}.srt"

    try:
        # 1. تحميل الفيديو من الرابط المباشر
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            response = client.get(video_url)
            if response.status_code != 200:
                raise Exception("فشل تحميل الفيديو من الرابط الموفر")
            with open(input_video, "wb") as f:
                f.write(response.content)

        # 2. تحديد ما إذا كانت الترجمة زمنية (تحتوي على '|' و '-') أو ثابتة
        is_timed = "|" in sub_text and "-" in sub_text

        if not is_timed:
            # ترجمة ثابتة: نبني SRT بسطر واحد يغطي الفيديو كله ونحرقه بنفس فلتر subtitles
            srt_content = _build_static_srt(input_video, sub_text)
        else:
            srt_content = convert_to_srt(sub_text)
            if not srt_content:
                raise Exception("فشل تحليل أوقات الترجمة المكتوبة، تأكد من مطابقة التنسيق 00:01 - 00:03 | النص")

        with open(srt_file, "w", encoding="utf-8") as f:
            f.write(srt_content)

        # subtitles + libass بيدعم تشكيل العربي وRTL بشكل صحيح، على عكس drawtext
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-i", input_video,
            "-vf", f"subtitles={srt_file}:force_style='FontName={ARABIC_FONT_FAMILY},Alignment=2,FontSize=20'",
            "-c:a", "copy", "-preset", "ultrafast", output_video,
        ]

        # 3. تشغيل معالجة الـ FFmpeg
        subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # 4. تحديث حالة العملية بنجاح — لا نُرجع الملف هنا، فقط نُعلم أنه جاهز
        _sub_jobs[job_id] = {
            "status": "done",
            "result_file_path": output_video,
            "related_files": [input_video, srt_file],
        }

    except subprocess.CalledProcessError as e:
        _sub_jobs[job_id] = {"status": "error", "reason": f"فشل ffmpeg: {e.stderr.decode(errors='ignore')[:300]}"}
        for file in [input_video, srt_file, output_video]:
            if os.path.exists(file):
                os.remove(file)
    except Exception as e:
        _sub_jobs[job_id] = {"status": "error", "reason": str(e)}
        for file in [input_video, srt_file, output_video]:
            if os.path.exists(file):
                os.remove(file)


def register(app):
    """تسجيل الـ Endpoints تلقائياً في FastAPI الخاص بالـ Space"""

    @app.post("/subtitler/create")
    def create_sub_job(req: SubtitleRequest, background_tasks: BackgroundTasks):
        job_id = f"sub_{uuid.uuid4().hex[:12]}"
        _sub_jobs[job_id] = {"status": "pending"}
        background_tasks.add_task(_process_video_subtitles, job_id, req.video_url, req.sub_text)
        return {"job_id": job_id, "status": "pending"}

    @app.get("/subtitler/status/{job_id}")
    def get_sub_job_status(job_id: str):
        """يرجّع JSON فقط دائماً — لا فيديو هنا أبداً. هذا يحل التضارب مع axios polling."""
        job = _sub_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="العملية غير موجودة")

        if job["status"] == "done":
            return {"status": "done", "download_url": f"/subtitler/download/{job_id}"}
        if job["status"] == "error":
            return {"status": "error", "reason": job.get("reason", "خطأ غير معروف")}
        return {"status": "pending"}

    @app.get("/subtitler/download/{job_id}")
    def download_sub_job(job_id: str):
        """Endpoint منفصل تماماً لتحميل الفيديو الفعلي، ويُستدعى مرة واحدة فقط بعد اكتمال المعالجة."""
        job = _sub_jobs.get(job_id)
        if not job or job.get("status") != "done":
            raise HTTPException(status_code=404, detail="الملف غير جاهز أو غير موجود")

        from fastapi.responses import FileResponse

        file_path = job["result_file_path"]
        related_files = job.get("related_files", [])

        class DeleteOnCloseFileResponse(FileResponse):
            def __init__(self, path: str, **kwargs):
                super().__init__(path, **kwargs)
                self._path_to_delete = path
                self._related_files = related_files

            def __del__(self):
                for f in [self._path_to_delete] + list(self._related_files):
                    if os.path.exists(f):
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                _sub_jobs.pop(job_id, None)

        return DeleteOnCloseFileResponse(file_path, media_type="video/mp4", filename="subtitled_video.mp4")
