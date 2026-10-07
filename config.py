"""إعدادات مركزية (تُقرأ من متغيرات البيئة)."""
import os
from pathlib import Path

VERSION = "1.2.0"
PROTOCOL = 2          # يتحقق منه العميل؛ يختلف إن كان الـ Space يشغّل كوداً آخر
PORT = int(os.getenv("PORT", "7860"))

MODELS_DIR = Path(os.getenv("MODELS_DIR", str(Path.home() / "models")))
VOICES_DIR = MODELS_DIR / "piper"
NLLB_REPO = "JustFrederik/nllb-200-distilled-600M-ct2-int8"
NLLB_DIR = MODELS_DIR / "nllb-200-distilled-600M-ct2-int8"

WHISPER_LEVELS = ["tiny", "base", "small"]          # من الأسرع إلى الأدق
WHISPER_DEFAULT = os.getenv("WHISPER_MODEL", "base")
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "ar_JO-kareem-medium")

API_KEY = os.getenv("API_KEY", "").strip()
DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
CPU_THREADS = int(os.getenv("CPU_THREADS", str(os.cpu_count() or 2)))

# --- الصوت الوارد ---
SR = 16000
MAX_UTT_SEC = 5.5        # أقصى طول للمقطع (أقصر = تأخير أقل)
MIN_SILENCE_SEC = 0.28   # وقفة تنهي المقطع
PAD_SEC = 0.2            # حشو قبل/بعد الكلام
MIN_SPEECH_SEC = 0.25    # أقصر كلام مقبول

# --- ملفات الأداء: «fast» يقلل التأخير (بلا دمج جمل، مقاطع قصيرة، ترجمة greedy) ---
PROFILES = {
    "fast":     {"max_utt": 3.5, "min_silence": 0.20, "merge": False, "beam": 1},
    "accurate": {"max_utt": 5.5, "min_silence": 0.28, "merge": True,  "beam": 2},
}
DEFAULT_PROFILE = os.getenv("DEFAULT_PROFILE", "fast")

# --- دمج الجمل (ملف accurate فقط) ---
MERGE_MAX_SEC = 4.0      # لا ننتظر دمجاً بعد هذا الطول
MERGE_GAP_SEC = 1.0      # أقصى فجوة بين مقطعين يمكن دمجهما

# --- المعالجة ---
MAX_IN_FLIGHT = 2        # مقاطع قيد الانتظار/المعالجة لكل جلسة
LS_MIN, LS_MAX = 0.7, 1.15   # حدود length_scale في Piper
RTF_DOWN = 0.8           # فوق هذا القيمة ثلاث مرات متتالية → خفض نموذج STT
RTF_UP = 0.3             # متوسط أقل من هذا → رفع النموذج
CACHE_MAX_BYTES = 50 * 1024 * 1024
