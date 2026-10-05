---
title: YouTube Arabic Dubbing
emoji: 🎙️
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# YouTube Arabic AI Dubbing: الخادم 1.1 (Hugging Face Docker Space)

> **مهم:** الإضافة 1.1 تتحقق من `protocol: 2` في `/health`. إن كان الـ Space يشغّل كوداً آخر ترفض الاتصال برسالة واضحة. بعد الرفع افتح `/health` وتأكد من `"protocol":2` و`"version":"1.1.0"`.

خادم FastAPI/Uvicorn **بدون Gradio**: يستقبل صوتاً حياً (PCM16 ‏16kHz) عبر WebSocket، ويعيد دبلجة عربية:
`VAD ← faster-whisper ← NLLB-200 (CT2 int8) ← Piper (Kareem)`.

## الرفع إلى Hugging Face (خطوة بخطوة)
1. أنشئ Space جديداً: **New Space ← SDK: Docker ← Blank ← Hardware: CPU basic (مجاني)**، ويفضّل **Public**.
2. ارفع **محتويات** هذا المجلد كما هي إلى جذر الـ Space (هذا الملف `README.md` بما فيه رأس YAML يجب أن يكون في الجذر).
   - عبر المتصفح: *Files ← Add file ← Upload files*، أو عبر git: `git clone https://huggingface.co/spaces/USER/NAME` ثم انسخ الملفات وادفعها.
3. انتظر البناء (Build) ‏— أول بناء ينزّل نحو 1.5GB من النماذج (وقتاً طويلاً نسبياً) وتُخبَّأ داخل الصورة.
4. اختبر: افتح `https://USER-NAME.hf.space/health` ← يجب أن ترى `"status":"ok"`.
5. (اختياري) **Settings ← Variables and secrets**: أضف Secret باسم `API_KEY` ثم أدخل نفس القيمة في إعدادات الإضافة.
   - Space العام + `API_KEY` هو الحل المعتمد لأن WebSocket من المتصفح لا يرسل ترويسة `Authorization`.

## متغيرات البيئة (كلها اختيارية)
| المتغير | الافتراضي | الوصف |
|---|---|---|
| `API_KEY` | فارغ | إن وُجد: يُطلب في رسالة `hello` (وليس في الـ URL) |
| `WHISPER_MODEL` | `base` | المستوى الابتدائي (`tiny`/`base`/`small`) |
| `DEFAULT_VOICE` | `ar_JO-kareem-medium` | |
| `ALLOWED_ORIGINS` | فارغ | قائمة Origins مسموحة للـ WebSocket، مثل `chrome-extension://ID` |
| `CORS_ORIGINS` | `*` | لـ `/health` و`/voices` |
| `DEBUG` | `false` | يضيف النصوص (`text_src`/`text_ar`) للردود ويفصّل السجلات |
| `CPU_THREADS` | عدد الأنوية | |

بناء أخف: أضف في Dockerfile `ARG WHISPER_MODELS="tiny base"` (يوفّر ≈ 0.5GB).

## نقاط النهاية والبروتوكول
- `GET /health`، `GET /voices`، `WS /ws`.
- **العميل → الخادم:** `hello` (JSON) ثم إطارات ثنائية `[uint32 session][uint32 sample_offset] + PCM16` (little-endian)، و`seek`، و`ping`، و`bye`.
  - **دعم السرعة:** `hello`/`seek` يحملان `rate` (سرعة الفيديو) و`speed` (سرعة النطق). **إضافة على الخطة:** رسالة `resync {session, media_t, sample_offset}` لتصحيح انحراف الزمن بعد الإيقاف/الاستئناف.
- **الخادم → العميل:** `ready`، `segment` (JSON) يتبعه إطار ثنائي `[uint32 session][uint32 seq][uint32 sample_rate] + PCM16`، و`silence {src_start, src_end}` (يدل أيضاً على تقدّم المعالجة)، و`quality`، و`error`، و`pong`.
- أي رد بجلسة مختلفة يتجاهله العميل؛ والخادم يتجاهل إطارات الجلسات القديمة.

## ما تم التحقق منه / ما لم يُتحقق منه
- ✔ ثُبّتت الحزم في `requirements.txt` فعلاً على **Python 3.12** (وهو ما يستخدمه Dockerfile بدل 3.11، لأن أحدث `numpy` المثبّت قد لا يدعم 3.11) وفُحصت واجهات `piper-tts` و`faster-whisper`.
- ✔ جرى اختبار المقسّم، وتحويل الأرقام، وبروتوكول WebSocket الكامل بمحركات وهمية.
- ✔ وُجود ملفات النماذج على Hugging Face: `JustFrederik/nllb-200-distilled-600M-ct2-int8` و`rhasspy/piper-voices/ar/ar_JO/kareem/{low,medium}`.
- ✘ **لم يُجرَّ بناء Docker كامل ولا تشغيل النماذج الحقيقية** (بيئة التطوير لا تصل إلى huggingface.co). شغّل `tools/bench.py` على الـ Space (Phase 0 ‏D) و`tools/ws_test.py` (Phase 0 ‏C) للقياس.

## ملاحظات وقيود
- **الترجمة جملة بجملة:** NLLB لا يستخدم سياقاً، لذلك لم يُنفَّذ بند «سياق آخر جملتين»؛ عُوِّض بدمج الجمل الناقصة (Sentence-aware).
- **الـ VAD:** القطع (endpointing) بكشف طاقة متكيّف، وSilero VAD المدمج في faster-whisper يُطبَّق داخل كل مقطع (`vad_filter=True`). الموسيقى المستمرة قد تنتج قطعاً قسرياً كل ≈ 8 ث.
- جودة Piper العربية (Kareem) متوسطة وبلا تشكيل.
- الـ Space المجاني ينام عند الخمول ويحتاج إيقاظاً (زر «إيقاظ الخادم» في الإضافة).
- **التراخيص:** `piper-tts` ‏GPL-3.0 (الخادم عندك فقط)، نموذج NLLB **CC-BY-NC-4.0 (غير تجاري)**، أصوات Piper وفق بطاقة كل صوت.
