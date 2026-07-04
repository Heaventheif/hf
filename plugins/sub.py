import os
import uuid
import httpx
import subprocess
from typing import List, Optional
from fastapi import BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

# معلومات وحزم النظام المطلوبة للـ plugin
DESCRIPTION = "إضافة ترجمة نصية (ثابتة أو زمنية) على مقاطع الفيديو مع تحكم بموضع كل سطر عمودياً"
# fonts-noto-core لا يكفي للعربي (مش فيه Naskh/Sans Arabic) + libass محتاج fontconfig
DOCKERFILE_DEPS = ["ffmpeg", "fonts-noto-core", "fonts-noto-ui-core", "fontconfig"]

# اسم عائلة الخط العربي المستخدم في الحرق (يجب أن يكون مثبتاً فعلياً في الـ Docker)
ARABIC_FONT_FAMILY = "Noto Naskh Arabic"

# ذاكرة مؤقتة لمتابعة حالة معالجة الفيديوهات في الخلفية
_sub_jobs = {}

# ═══════════════════════════════════════════════════════════════
# خريطة تموضع محور Y — نسبة مئوية من ارتفاع الفيديو الكلي (بند 1)
# ═══════════════════════════════════════════════════════════════
Y_POSITION_PERCENT = {
    1: 0.10,  # أعلى الشاشة
    2: 0.30,
    3: 0.50,  # المنتصف
    4: 0.75,  # الافتراضي
    5: 0.90,  # أسفل الشاشة
}
DEFAULT_POSITION = 4


class SubtitleCue(BaseModel):
    """مقطع ترجمة واحد — نص + موضعه العمودي + توقيته الاختياري."""
    position: int = Field(DEFAULT_POSITION, ge=1, le=5, description="موضع عمودي من 1 (أعلى) إلى 5 (أسفل)")
    start: Optional[float] = Field(None, description="وقت الظهور بالثواني، أو None = من بداية الفيديو")
    end: Optional[float] = Field(None, description="وقت الاختفاء بالثواني، أو None = حتى نهاية الفيديو")
    text: str = Field(..., min_length=1)


class SubtitleRequest(BaseModel):
    video_url: str = Field(..., description="رابط تحميل الفيديو المباشر من مسنجر/ريندر")
    cues: List[SubtitleCue] = Field(..., min_length=1, description="قائمة مقاطع الترجمة")


def _srt_escape(text: str) -> str:
    """تنظيف بسيط للنص من أي أسطر فاضية أو محارف قد تكسر تحليل الـ SRT."""
    return text.replace("\r", "").strip()


def _seconds_to_srt_ts(seconds: float) -> str:
    """يحوّل عدد ثوانٍ (float) إلى توقيت SRT بصيغة HH:MM:SS,mmm."""
    seconds = max(seconds, 0.0)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _probe_video_info(video_path: str) -> tuple[float, int, int]:
    """يجلب مدة الفيديو (ثانية) وعرضه وارتفاعه الحقيقيين عبر ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "default=noprint_wrappers=1",
            video_path,
        ],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    info = {}
    for line in result.stdout.decode().strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()

    duration = float(info.get("duration", "0") or 0)
    width = int(float(info.get("width", "0") or 0))
    height = int(float(info.get("height", "0") or 0))

    if not width or not height:
        raise Exception("تعذّر قراءة أبعاد الفيديو عبر ffprobe")

    # هامش أمان بسيط لضمان بقاء آخر ترجمة ظاهرة حتى آخر إطار
    duration = max(duration - 0.05, 0.1)
    return duration, width, height


def _build_srt(cues: List[SubtitleCue], video_duration: float, width: int, height: int) -> str:
    """
    يبني ملف SRT كامل من قائمة الـ cues، مع حقن أكواد تموضع ASS
    (\\an5\\pos(x,y)) داخل نص كل سطر — مدعومة من libass حتى ضمن حاويات SRT
    عند استخدام فلتر subtitles في ffmpeg (بند 1: التموضع العمودي لكل سطر).

    \\an5 = التثبيت في منتصف نقطة الإحداثيات أفقياً وعمودياً، فنحصل بذلك على:
      - توسيط أفقي دائم (منتصف عرض الفيديو) — بند 1
      - ارتفاع عمودي متغيّر بحسب موضع كل سطر (1 إلى 5) — بند 1
    """
    srt_output = ""
    x_center = width // 2

    for index, cue in enumerate(cues, start=1):
        y_percent = Y_POSITION_PERCENT.get(cue.position, Y_POSITION_PERCENT[DEFAULT_POSITION])
        y = int(height * y_percent)

        # بند 2: توقيت الظهور — ثابت لكامل الفيديو إن لم يُحدَّد
        start_sec = cue.start if cue.start is not None else 0.0
        end_sec = cue.end if cue.end is not None else video_duration

        start_ts = _seconds_to_srt_ts(start_sec)
        end_ts = _seconds_to_srt_ts(min(end_sec, video_duration))

        text = _srt_escape(cue.text)
        positioned_text = f"{{\\an5\\pos({x_center},{y})}}{text}"

        srt_output += f"{index}\n{start_ts} --> {end_ts}\n{positioned_text}\n\n"

    return srt_output


def _process_video_subtitles(job_id: str, video_url: str, cues: List[SubtitleCue]):
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

        # 2. قراءة أبعاد الفيديو الحقيقية ومدته — ضروريان لحساب موضع Y الدقيق لكل سطر
        duration, width, height = _probe_video_info(input_video)

        # 3. بناء SRT واحد يحتوي كل الـ cues، كل سطر بموضعه وتوقيته الخاصين
        srt_content = _build_srt(cues, duration, width, height)
        if not srt_content:
            raise Exception("فشل بناء ملف الترجمة — لا توجد مقاطع صالحة")

        with open(srt_file, "w", encoding="utf-8") as f:
            f.write(srt_content)

        # subtitles + libass يدعم تشكيل العربي وRTL بشكل صحيح، على عكس drawtext.
        # original_size=WxH يضمن أن إحداثيات \pos() المحسوبة أعلاه تُطابق أبعاد
        # الفيديو الفعلية تماماً بدل أن يقوم libass بافتراض دقة افتراضية مختلفة.
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-i", input_video,
            "-vf",
            (
                f"subtitles={srt_file}:original_size={width}x{height}:"
                f"force_style='FontName={ARABIC_FONT_FAMILY},FontSize=20'"
            ),
            "-c:a", "copy", "-preset", "ultrafast", output_video,
        ]

        # 4. تشغيل معالجة الـ FFmpeg
        subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # 5. تحديث حالة العملية بنجاح — لا نُرجع الملف هنا، فقط نُعلم أنه جاهز
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
        background_tasks.add_task(_process_video_subtitles, job_id, req.video_url, req.cues)
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
