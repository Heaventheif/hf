---
title: SunkenBot — البوابة الموحّدة
emoji: 🐙
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# g — البوابة الموحّدة (Go + Python في فضاء واحد)

بعد هذا التطوير، يعمل **g** و **s** في **نفس الفضاء/الحاوية** بدل فضاءين منفصلين، ويصبح **g** هو البوابة الوحيدة التي يكلّمها **SunkenBot**.

## البنية

```
Render (فضاء واحد، منفذ واحد 7860)
│
├── Go  (g) ──────────────────────────────────────────── المنفذ 7860
│     ├─ مسارات سريعة تُحسَب في Go مباشرة:
│     │    gemini, groq, comic, novel, sub, manga-bridge,
│     │    pinterest, ping
│     └─ مسارات ثقيلة تُوَّكل (proxy خام حرفي) إلى Python:
│          chess (/process_move), dama, ocr
│
└── Python (s) ───────────────────────────────────────── المنفذ 8001 (داخلي فقط)
      chess, dama, ocr — لا يراها أحد من الخارج
```

SunkenBot لا يتغيّر فيه شيء جذرياً: يكلّم منفذاً واحداً (`HF_SPACE_URL` من متغيرات البيئة) والمسارات كلها كما كانت.

## لماذا proxy خام (raw body)؟

بعض الـ endpoints الموكلة تستقبل `multipart/form-data` (صور OCR) أو أجساماً لا نعرف بنيتها من Go. إعادة إرسال الجسم **حرفياً** يضمن تطابقاً تاماً مع ما كانت Python تستقبله سابقاً من Render مباشرة، دون ازدواجية في تعريف request structs.

## إضافة أمر/مسار جديد — ثلاث حالات

### 1. أمر جديد في Go (سريع) — حزمة جديدة + سطرين في main.go

أنشئ `plugins/<اسم>/<اسم>.go` ينفّذ عقد `plugins.Service` الوحيد (`Name` + `Routes`)، ثم **سطرين** في `main.go`:

```go
// main.go
svcs := []plugins.Service{
    ...
    mysvc.New(shared.Client),              // ← سطر التسجيل
}
descriptions["mysvc"] = mysvc.Description  // ← سطر الوصف
```

الالتزام بعقد `Service` هو القيد الوحيد — لا `init()` صامت ولا reflection ولا blank imports.

### 2. أمر جديد في Python (ثقيل) — ملف واحد فقط

أنشئ `python-services/plugins/<اسم>.py` ينفّذ عقد `Plugin` بنفس نمط s القديم (`routes()`, `startup()`, `requirements = [...]`, `pip_extra = [...]`) — سيُكتشف تلقائياً، وتُجمَع متطلباته تلقائياً عبر `collect_requirements.py` في بناء Docker. **لا تعديل في أي مكان آخر.**

### 3. endpoint موجود في Python تريد تشغيله عبر البوابة — سطر واحد

أضف سطراً في مصفوفة `[]delegate.Endpoint` في `plugins/delegated/delegated.go`:

```go
{Method: "POST", Path: "/dama/new_game", Timeout: 2 * time.Minute},
```

المهلة الافتراضية 90 ثانية لكل endpoint بلا مهلة صريحة. التسجيل في `main.go` happens تلقائياً.

## متغيرات البيئة

| المتغيّر | الوصف |
|---|---|
| `PORT` | المنفذ الخارجي (افتراضي `7860`) |
| `HF_SPACE_URL` | رابط الفضاء الخارجي — **يستخدمه SunkenBot فقط** من متغيرات البيئة (لم يعد مضمّناً في كود الأوامر) |
| `INTERNAL_TOKEN` | توكن الحماية بين Render↔Go و Go↔Python (نفس القيمة على الجانبين) |
| `HF_PYTHON_URL` | يُصدَّر تلقائياً من `run.sh` بقيمة `http://localhost:8001` — **لا تضبطه يدوياً** |
| `GEMINI_API_KEY` / `_2` / `_3` / `_4` | مفاتيح Gemini (تدوير عند نفاد الحصة) |
| `GROQ_API_KEY` | مفتاح Groq |
| `FERDEV_API_KEY` | مفتاح احتياطي لـ Pinterest عند فشل الكشط المباشر |
| `DATABASE_URL` | اختياري — NeonDB، يتطلب بناءً بـ `-tags postgres` وإلا يُتجاهَل بصمت (يجب أن ينتهي بـ `?sslmode=require`) |
| `SESSION_HISTORY_LIMIT` | اختياري — عدد الرسائل المحفوظة لكل محادثة (افتراضي `40`) |
| `STOCKFISH_PATH` | مسار ثنائي Stockfish البديل (افتراضي: يبحث في `PATH`) |

## البناء والتشغيل

```bash
docker build -t sunkenbot-unified .
docker run --rm -e INTERNAL_TOKEN=... -e GEMINI_API_KEY=... \
           -p 7860:7860 sunkenbot-unified
```

`run.sh` يرفع Python أولاً على `127.0.0.1:8001` (خلفية)، ثم ينفّذ Go (يحجز 7860 ويصبح PID 1). عند توقف Go لأي سبب تتوقف Python معه تلقائياً (لا معنى لبقاء Python وحدها بدون بوابتها الخارجية).

## فحص الصحة

`GET /health` كما كان دائماً. إضافة إلى ذلك، فحص دوري في الخلفية لاستجابة Python: لو لم تستجب تُسجَّل تحذيرات واضحة، والمسارات الموكلة تعطي `502` برسالة مفهومة بدل صمت. وإذا لم يكن `HF_PYTHON_URL` قابلاً للوصول عند الإقلاع **لا تُسجَّل** المسارات الموكلة إطلاقاً — أفضل من endpoints تعطي 502 دائماً بصمت.

## الاختبار المحلي

```bash
cd g && export PATH=$PATH:/usr/local/go/bin
go mod tidy && go build -tags postgres -o sunkenbot .
go vet ./... && go test ./...
INTERNAL_TOKEN=secret ./sunkenbot
```

## ملاحظات معمارية موروثة (لا تحذفها)

- `go build -tags postgres`: `internal/session/postgres_real.go` يُبنى فقط مع الوسم (NeonDB). البناء بدونه يعمل لكن الجلسات تبقى في الذاكرة. راجع `postgres_stub.go` مقابل `postgres_real.go`. استخدم `-tags postgres` دائماً في أي بيئة تريد تخزيناً دائماً.
- أسطر `replace` في `go.mod` (مرايا GitHub لنطاقات `golang.org`) ضرورية لبيئات شبكة مقيّدة — لا تحذفها دون التأكد أن بيئة البناء تصل لتلك النطاقات مباشرة. `github.com/jackc/pgx/v5` تُضاف تلقائياً عبر `go mod tidy`.
- `ENV PATH="${PATH}:/usr/games"`: `stockfish` من مستودعات Debian تُثبَّت في `/usr/games` غير المدرج في PATH الافتراضي — بدون هذا السطر: `executable file not found in $PATH`.
- `HEALTHCHECK` و`USER sunkenbot` غير-root (آمن: `chromedp.NoSandbox` دائماً بلا شرط).
- مجلد `/app/data/manga_bridge/images` يُنشأ بصلاحيات `sunkenbot` في الـ Dockerfile — راجع `imagesSubdir` في `plugins/mangabridge`.
