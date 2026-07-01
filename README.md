---
title: Sunken Bot
emoji: 🤖
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
---
# 🤖 Sunken Bot — Universal Bot API

<div align="center">

**واجهة برمجية موحّدة متعددة الخدمات مبنية على FastAPI — مُصمَّمة للنشر على Hugging Face Spaces**

![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-latest-green?logo=fastapi)
![Docker](https://img.shields.io/badge/Docker-ready-blue?logo=docker)
![HuggingFace](https://img.shields.io/badge/HuggingFace-Space-yellow?logo=huggingface)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

</div>

---

## 📋 نظرة عامة

**Sunken Bot** هو خادم API موحَّد يجمع عشرات الخدمات المختلفة تحت سقف واحد: نماذج ذكاء اصطناعي متعددة، تحميل وسائط، قراءة روايات، بحث صور، وغيرها. يعمل النظام على Hugging Face Spaces باستخدام Docker ويُزامَن تلقائياً من GitHub.

المميز في هذا النظام أن بنيته مبنية على نظام **plugins** قابل للتوسعة: أي ميزة جديدة تُضاف بمجرد إضافة ملف Python واحد إلى مجلد `plugins/` دون أي تعديل على الكود الأساسي.

---

## 🏗️ بنية المشروع

```
hf-space/
├── main.py                    # نقطة دخول FastAPI — ثابتة، لا تُعدَّل
├── plugin_loader.py           # محرك تحميل الـ plugins تلقائياً
├── proxy_client.py            # عميل HTTP يوجّه الطلبات عبر Cloudflare Worker
├── Dockerfile                 # صورة Docker للنشر على HF Spaces
├── requirements.txt           # المكتبات الأساسية
│
├── plugins/                   # كل الميزات — أضف ملفاً جديداً وانتهى
│   ├── cerebras.py            # نموذج Cerebras GPT-OSS (جلسات جماعية)
│   ├── chess.py               # بوت الشطرنج
│   ├── fb.py                  # تحميل فيديوهات فيسبوك/ريلز
│   ├── gemini.py              # Gemini 2.5 Flash مع Google Search Grounding
│   ├── gptx.py                # GPT-4o عبر GitHub Models + دعم الصور
│   ├── groq.py                # Llama 4 Scout (نص + صوت + صورة + فيديو)
│   ├── hf.py                  # HuggingFace Inference API (20+ نموذج)
│   ├── image.py               # توليد الصور (FLUX / SDXL)
│   ├── mood_sticker.py        # GIF مناسب لمزاج الأغنية
│   ├── novel.py               # قراءة الروايات (5 مصادر)
│   ├── pinterest.py           # بحث صور Pinterest
│   ├── quran.py               # آيات قرآنية مع التفسير
│   ├── random.py              # فيديو عشوائي من Tumblr
│   ├── sing.py                # بحث وتحميل من SoundCloud
│   └── translate.py           # ترجمة عبر Google Translate
│
├── bot_chess/
│   └── chess_engine.py        # محرك الشطرنج (minimax + evaluation)
│
├── scrapers/
│   └── wtr_lab.py             # كاشط روايات wtr-lab
│
└── .github/
    └── workflows/
        ├── sync.yml           # مزامنة تلقائية GitHub → HF Space
        └── keep-alive.yml     # إبقاء الـ Space مستيقظاً يومياً
```

---

## ⚡ نظام الـ Plugins

### كيف يعمل؟

عند بدء تشغيل الخادم، يقوم `plugin_loader.py` تلقائياً بـ:

1. **اكتشاف** كل ملفات `plugins/*.py`
2. **تثبيت** متطلبات كل plugin من `plugins/requirements/<name>.txt` (إن وُجد)
3. **تحميل** الـ module ديناميكياً
4. **تسجيل** الـ routes على تطبيق FastAPI عبر دالة `register(app)` أو `setup(app)`
5. **تحديث** `requirements.txt` الجذر و`Dockerfile` تلقائياً بأي متطلبات جديدة

فشل تحميل plugin واحد **لا يوقف** باقي الـ plugins.

### كتابة Plugin جديد

```python
# plugins/my_feature.py

DESCRIPTION = "وصف مختصر للـ plugin"
DOCKERFILE_DEPS = ["ffmpeg"]  # اختياري — packages تُضاف لـ apt-get

def register(app):

    @app.get("/my-endpoint")
    async def my_endpoint():
        return {"status": "ok"}
```

هذا كل شيء. أضف الملف وسيُكتشف تلقائياً عند إعادة تشغيل الخادم.

---

## 🔌 الـ Endpoints المتاحة

### 🤖 الذكاء الاصطناعي

#### `POST /groq` — Llama 4 Scout (الأقوى والأشمل)

النموذج الرئيسي. يدعم النص والصور والصوت والفيديو مع جلسات جماعية محفوظة في MongoDB وـ Gemini كـ fallback.

```json
// طلب نصي عادي
{
  "thread_id": "group_xyz",
  "sender_name": "Ahmed",
  "prompt": "ما هو الذكاء الاصطناعي؟"
}

// طلب مع صورة
{
  "thread_id": "group_xyz",
  "sender_name": "Ahmed",
  "prompt": "ما هذه الصورة؟",
  "attachment": {
    "kind": "image",
    "url": "https://example.com/photo.jpg"
  }
}

// طلب مع صوت
{
  "thread_id": "group_xyz",
  "sender_name": "Ahmed",
  "prompt": "لخّص ما قيل",
  "attachment": {
    "kind": "audio",
    "url": "https://example.com/voice.mp3"
  }
}

// مسح ذاكرة المجموعة
{
  "thread_id": "group_xyz",
  "clear": true
}
```

**الاستجابة:**
```json
{
  "reply": "الذكاء الاصطناعي هو...",
  "provider": "groq | groq-vision | groq-whisper | gemini-fallback"
}
```

---

#### `POST /gemini` — Gemini 2.5 Flash

يستخدم Google Search Grounding للحصول على معلومات حديثة. يدعم 4 مفاتيح API مع تناوب تلقائي عند نفاد الحصة. Groq كـ fallback.

```json
{
  "thread_id": "group_xyz",
  "sender_name": "Sara",
  "prompt": "ما آخر أخبار الذكاء الاصطناعي؟"
}
```

**الاستجابة:**
```json
{
  "reply": "...",
  "provider": "gemini | groq"
}
```

---

#### `POST /gptx` — GPT-4o

عبر GitHub Models API. يدعم الصور بشكل مباشر.

```json
{
  "thread_id": "group_xyz",
  "sender_name": "Ali",
  "prompt": "اشرح هذه الصورة",
  "image_url": "https://example.com/image.jpg"
}
```

**أو إرسال الصورة كـ base64:**
```json
{
  "thread_id": "group_xyz",
  "sender_name": "Ali",
  "prompt": "ما هذا؟",
  "image_b64": "data:image/jpeg;base64,/9j/4AAQ...",
  "image_type": "image/jpeg"
}
```

---

#### `POST /cerebras` — Cerebras GPT-OSS

نماذج مفتوحة المصدر سريعة الاستجابة.

```json
{
  "thread_id": "group_xyz",
  "sender_name": "Nour",
  "prompt": "اكتب قصيدة عن البحر",
  "model": "120b"
}
```

| قيمة `model` | النموذج الفعلي |
|---|---|
| `"120b"` | `gpt-oss-120b` (افتراضي) |
| `"20b"` | `gpt-oss-20b` |

---

#### `POST /hf` — HuggingFace Inference (20+ نموذج)

الوصول إلى عشرات النماذج عبر HuggingFace Inference API.

```json
{
  "thread_id": "group_xyz",
  "sender_name": "Maha",
  "prompt": "ترجم هذا النص للإنجليزية: مرحبا بالعالم",
  "model": "qwen72"
}
```

**النماذج المتاحة (اختصارات):**

| الاختصار | النموذج الكامل |
|---|---|
| `qwen` / `qwen72` | Qwen2.5-72B-Instruct |
| `qwen3` | Qwen3-235B-A22B |
| `llama4` | Llama-4-Scout-17B |
| `llama70` | Llama-3.3-70B-Instruct |
| `deepseek` | DeepSeek-R1-Distill-Qwen-32B |
| `gemma` | Gemma-3-27b-it |
| `phi4` | Microsoft Phi-4 |
| `mistral22` | Mistral-Small-3.1-22B |
| `command` | Cohere Command R+ |

> استعراض كل النماذج: `GET /hf/models`

---

### 🖼️ الصور

#### `POST /image` — توليد الصور

```json
{
  "prompt": "a beautiful sunset over the ocean, photorealistic",
  "model": "flux",
  "width": 1024,
  "height": 1024
}
```

**الاستجابة:**
```json
{
  "image_base64": "/9j/4AAQ...",
  "content_type": "image/jpeg",
  "model_used": "black-forest-labs/FLUX.1-schnell"
}
```

| اختصار | النموذج |
|---|---|
| `flux` | FLUX.1-schnell (افتراضي، الأسرع) |
| `flux-dev` | FLUX.1-dev (أعلى جودة) |
| `sdxl` / `sd` | SDXL-Turbo |

---

#### `POST /pinterest` — بحث صور

```json
{
  "query": "minimalist bedroom design",
  "limit": 5
}
```

**الاستجابة:**
```json
{
  "images": ["https://i.pinimg.com/...", "..."],
  "query": "minimalist bedroom design"
}
```

---

### 🎵 الصوت والموسيقى

#### `POST /sing/search` — البحث في SoundCloud

```json
{
  "query": "Fairuz يا طير"
}
```

**الاستجابة:**
```json
{
  "results": [
    {"title": "يا طير — فيروز", "url": "https://soundcloud.com/..."},
    ...
  ]
}
```

#### `POST /sing/download` — تحميل الأغنية

```json
{
  "url": "https://soundcloud.com/artist/track-name",
  "title": "اسم الأغنية"
}
```

**الاستجابة:**
```json
{
  "audio_b64": "SUQzBAAAAAAAI...",
  "title": "اسم الأغنية",
  "size": 4500000
}
```

---

#### `GET /stickers/mood?title=...` — ستيكر يناسب مزاج الأغنية

يحلّل اسم الأغنية بالذكاء الاصطناعي ويُرجع GIF مناسب من Giphy.

```
GET /stickers/mood?title=Blinding Lights
```

**الاستجابة:** ملف GIF مباشر (`image/gif`) مع header:
```
X-Mood-Category: energetic
```

---

### 📹 الفيديو

#### `POST /fb` — تحميل فيديو فيسبوك

يدعم: `/watch?v=...`، `/reel/...`، `/<page>/videos/...`، `fb.watch/...`

```json
{
  "url": "https://www.facebook.com/reel/1234567890",
  "quality": "worst"
}
```

**الاستجابة (ملف صغير ≤ 25MB):**
```json
{
  "video_b64": "AAAAIGZ0eX...",
  "title": "عنوان الفيديو",
  "size": 8500000
}
```

**الاستجابة (ملف كبير > 25MB):**
```json
{
  "video_url": "https://cdn.facebook.com/...",
  "title": "عنوان الفيديو"
}
```

#### `POST /random` — فيديو عشوائي من Tumblr

```json
{}
```

**الاستجابة:**
```json
{
  "video_b64": "...",
  "caption": "وصف الفيديو",
  "blog": "pleasantlytwisted",
  "size": 3200000
}
```

---

### ♟️ الشطرنج

#### `POST /process_move` — تحليل الحركة والرد

```json
{
  "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",
  "move": "e7e5",
  "bot_mode": "minimax",
  "difficulty": 3
}
```

**الاستجابة:**
```json
{
  "bot_move": "g1f3",
  "fen": "...",
  "is_checkmate": false,
  "is_stalemate": false,
  "evaluation": 0.15
}
```

---

### 📖 الروايات

#### `GET /novel/fetch?name=...&chapter=...` — قراءة فصل (5 مصادر بالتسلسل)

```
GET /novel/fetch?name=solo leveling&chapter=1
```

**الاستجابة:**
```json
{
  "success": true,
  "source": "wtr-lab",
  "elapsed_seconds": 2.1,
  "novel": {
    "title": "Solo Leveling",
    "url": "https://wtr-lab.com/en/novel/..."
  },
  "chapter": {
    "number": 1,
    "title": "Chapter 1: The Weakest Hunter",
    "paragraphs": ["...", "..."],
    "paragraph_count": 47,
    "url": "..."
  }
}
```

**المصادر بالترتيب:** wtr-lab → FanMTL → Novelbin → LightNovelWorld → LNMTL

#### `GET /novel/search?q=...` — البحث عن رواية (wtr-lab)
#### `GET /novel/chapter?id=...&slug=...&chapter=...` — قراءة فصل مباشر (wtr-lab)

---

### 🕌 القرآن الكريم

#### `POST /quran`

```json
{
  "surah": 2,
  "ayah": 255
}
```

**الاستجابة:**
```json
{
  "text": "ٱللَّهُ لَآ إِلَـٰهَ إِلَّا هُوَ...",
  "tafsir": "الله وحده لا شريك له في ألوهيته...",
  "meta": {
    "surah_name": "البقرة",
    "surah_english": "The Cow",
    "revelation": "Medinan",
    "juz": 3,
    "page": 42,
    "surah": 2,
    "ayah": 255
  }
}
```

---

### 🌍 الترجمة

#### `POST /translate`

```json
{
  "text": "Hello, how are you?",
  "to": "ar"
}
```

**الاستجابة:**
```json
{
  "result": "مرحبا، كيف حالك؟"
}
```

---

### 🏠 نقاط المعلومات

#### `GET /` — حالة الخادم وقائمة الـ plugins المحمَّلة

```json
{
  "status": "online",
  "plugins": {
    "groq":    {"status": "loaded", "routes_added": 1, "description": "..."},
    "gemini":  {"status": "loaded", "routes_added": 1, "description": "..."},
    "chess":   {"status": "error",  "reason": "ImportError: ..."},
    ...
  }
}
```

#### `GET /health` — فحص الصحة

```json
{
  "status": "healthy",
  "timestamp": 1719500000.0
}
```

---

## 🔑 متغيرات البيئة

أضفها في **Settings → Variables and secrets** في HF Space:

| المتغير | الاستخدام | إلزامي؟ |
|---|---|---|
| `GROQ_API_KEY` | Llama 4 Scout + Whisper + fallback في Gemini | موصى به |
| `GEMINI_API_KEY` | Gemini 2.5 Flash | موصى به |
| `GEMINI_API_KEY_2` / `_3` / `_4` | مفاتيح إضافية عند نفاد الحصة | اختياري |
| `HF_TOKEN` | HuggingFace Inference + توليد الصور | لـ `/hf` و `/image` |
| `GITHUB_MODELS_TOKEN` | GPT-4o عبر GitHub Models | لـ `/gptx` |
| `CEREBRAS_API_KEY` | Cerebras GPT-OSS | لـ `/cerebras` |
| `MONGO_URI` | حفظ جلسات المحادثة | اختياري (بدونه: بلا ذاكرة) |
| `TUMBLR_API_KEY` | فيديوهات عشوائية | لـ `/random` |
| `GIPHY_API_KEY` | GIF مزاج الأغنية | لـ `/stickers/mood` |
| `FERDEV_API_KEY` | SoundCloud + Pinterest | لـ `/sing` و `/pinterest` |
| `CF_WORKER_URL` | توجيه الطلبات عبر Cloudflare Worker | اختياري |
| `MOOD_AI_PROVIDER` | مزوّد تصنيف المزاج (`groq`\|`gemini`\|...) | اختياري (افتراضي: `groq`) |

---

## 🐳 Docker والنشر

### متطلبات النظام (Dockerfile)

الصورة مبنية على `python:3.11-slim` وتتضمن:

- مكتبات الرسوميات: `libcairo2`, `libpango`, `libgdk-pixbuf`
- خطوط: `fonts-noto-core` (لدعم العربية والآسيوية)
- Playwright/Chromium وكل dependencies له
- المنفذ: `7860` (HF Spaces standard)

### تشغيل محلي

```bash
git clone https://github.com/your-username/hf-space.git
cd hf-space

# إنشاء ملف .env
cp .env.example .env
# عدّل المتغيرات في .env

# بناء وتشغيل
docker build -t sunken-bot .
docker run -p 7860:7860 --env-file .env sunken-bot
```

بعدها افتح: `http://localhost:7860`

### تشغيل بدون Docker (للتطوير)

```bash
pip install -r requirements.txt
playwright install chromium

GROQ_API_KEY=your_key uvicorn main:app --reload --port 7860
```

---

## 🔄 المزامنة التلقائية مع HF Spaces

### GitHub Action: `sync.yml`

عند كل `push` لـ `main`، يُرسل الكود تلقائياً لـ HF Space.

**الإعداد المطلوب:**
1. أضف `HF_TOKEN` في **GitHub → Settings → Secrets → Actions**
2. عدّل `huggingface_repo_id` في `sync.yml` ليطابق اسم space الخاص بك:
   ```yaml
   huggingface_repo_id: YOUR_USERNAME/YOUR_SPACE_NAME
   ```

### GitHub Action: `keep-alive.yml`

يُرسل طلباً تلقائياً يومياً (12:00 ظهراً GMT) لإبقاء الـ Space مستيقظاً وعدم دخوله وضع السكون.

يمكن تشغيله يدوياً من: **GitHub → Actions → Keep Hugging Face Space Alive → Run workflow**

---

## 🛡️ الأمان والموثوقية

### نظام الـ Fallback

| الخدمة | الأساسي | الاحتياطي |
|---|---|---|
| `/groq` | Llama 4 Scout | Gemini 2.0 Flash |
| `/gemini` | Gemini 2.5 Flash | Groq Llama 3.3 70B |
| `/fb` | جودة عالية | جودة منخفضة تلقائياً |
| `/novel` | wtr-lab | fanmtl → novelbin → lnw → lnmtl |

### حدود الملفات

- الحد الأقصى لتحميل الفيديو/الصوت: **25MB**
- إذا تجاوز الملف الحد، يُرجع الـ API رابط التحميل المباشر بدلاً من الملف

### فلترة روابط فيسبوك

يرفض `/fb` تلقائياً:
- روابط المنشورات (`/posts/`)
- الصور (`/photo/`, `/photos/`)
- الـ Marketplace والفعاليات
- روابط البروفايل الشخصي

ويقبل فقط روابط الفيديوهات الحقيقية.

### Cloudflare Proxy

يمكن توجيه كل الطلبات الخارجية عبر Cloudflare Worker لتجنب حظر IP:

```python
from proxy_client import proxy_get

response = proxy_get("https://some-api.com/data", timeout=15)
```

---

## 🗃️ MongoDB والجلسات

الـ plugins التالية تحفظ سياق المحادثة في MongoDB:
`/groq`, `/gemini`, `/gptx`, `/cerebras`, `/hf`

**سلوك الجلسات:**
- يحفظ آخر **10 رسائل** لكل `thread_id`
- إذا لم يُضبط `MONGO_URI`، يعمل الـ API بدون ذاكرة (stateless)
- لمسح ذاكرة مجموعة: أرسل `"clear": true`

**مخطط المجموعات في قاعدة البيانات:**
```
db: sunken
  ├── groq_sessions
  ├── gemini_sessions
  ├── gptx_sessions
  ├── cerebras_sessions
  └── hf_sessions
```

كل وثيقة:
```json
{
  "_id": "thread_id_هنا",
  "messages": [...],
  "updated_at": "2024-01-01T12:00:00Z"
}
```

---

## 🔧 التوسعة والتطوير

### إضافة نموذج لـ `/hf`

في `plugins/hf.py`، أضف اختصار النموذج لقاموس `SHORTCUTS`:

```python
SHORTCUTS = {
    ...
    "my_model": "organization/model-name-on-hf",
}
```

### إضافة نموذج يدعم الصور لـ `/hf`

أضفه أيضاً لـ `VISION_MODELS`:
```python
VISION_MODELS = {
    ...
    "organization/model-name-on-hf",
}
```

### إضافة متطلبات خاصة لـ Plugin

أنشئ ملفاً في `plugins/requirements/<plugin_name>.txt`:
```
# plugins/requirements/my_feature.txt
some-library>=1.0.0
another-package
```

سيُثبَّت تلقائياً عند بدء تشغيل الخادم.

### إضافة متطلبات apt-get

في ملف الـ plugin:
```python
DOCKERFILE_DEPS = ["ffmpeg", "libsndfile1"]
```

سيُضافان تلقائياً لـ `Dockerfile` عند التشغيل.

---

## 📊 مثال على تدفق طلب `/groq` بالكامل

```
Client
  │
  ▼
POST /groq  {"thread_id": "g1", "sender_name": "Ali", "prompt": "ما الطقس؟"}
  │
  ▼
plugin_loader.py (plugins/groq.py تم تحميله مسبقاً)
  │
  ▼
MongoDB: تحميل آخر 10 رسائل للـ thread_id "g1"
  │
  ▼
بناء messages = [system_prompt, ...context, user_message]
  │
  ▼
Groq API (Llama 4 Scout)
  ├── نجاح → reply
  └── فشل → Gemini 2.0 Flash fallback
              ├── نجاح → reply
              └── فشل → 503 "كل الخوادم فشلت"
  │
  ▼
MongoDB: حفظ الرسالة والرد
  │
  ▼
{"reply": "الطقس اليوم...", "provider": "groq"}
```

---

## 📜 الترخيص

هذا المشروع مرخَّص بموجب رخصة **MIT** — راجع ملف [LICENSE](LICENSE) للتفاصيل.

---

## 🤝 المساهمة

1. افتح **Issue** لوصف الميزة أو الخطأ
2. أنشئ **Fork** للمستودع
3. أضف الـ plugin الجديد في `plugins/`
4. أرسل **Pull Request**

> **تذكير:** `main.py` و`plugin_loader.py` ثابتان ولا يُعدَّلان. كل الإضافات تتم عبر `plugins/` فقط.
