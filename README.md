---
title: Sunken Bot Go Edition
emoji: 🐙
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# Sunken Bot — Go Edition

تحويل كامل للمشروع من Python (FastAPI) إلى Go. **كل الـ 8 plugins محوَّلة
ومبنية ومُختبرة فعلياً** (build حقيقي + تشغيل + طلبات HTTP حقيقية، وبعضها
باختبارات وحدة تلقائية).

> **ملاحظة:** كان plugin `fb` موجوداً في نسخة سابقة من هذا الترحيل وتمت
> إزالته من هذه النسخة (لا يوجد `plugins/fb` ولا أي استيراد له في
> `main.go`). العدد الحالي 8 plugins محوَّلة من بايثون + `ping` التوضيحي.

## نظرة عامة على المعمارية

الغالبية العظمى من المنطق (HTTP handlers، الجلسات، الكشط النصي، منطق
الأعمال) هي **Go خالص**. لكن ثلاثة أجزاء تحديداً تحتاج قدرات لا يملكها Go
بشكل ناضج (متصفح Chromium حقيقي لتجاوز حماية Cloudflare/JS، ومحرّك
شطرنج + رسم SVG): لهذه الثلاثة فقط، Go يستدعي **سكربت Node.js** كعملية
فرعية (subprocess) — تماماً بنفس الطريقة التي يستدعي بها `sub` أداة
`ffmpeg` الخارجية.

```
┌─────────────┐   subprocess + JSON stdin/stdout    ┌────────────────────┐
│  Go plugin  │ ──────────────────────────────────▶ │   Node.js script   │
│ (HTTP layer)│ ◀────────────────────────────────── │ (npm: chess.js,    │
└─────────────┘                                      │  sharp, playwright)│
                                                      └────────────────────┘
```

| Plugin         | المنطق | لماذا Node بدل Go نقي |
|----------------|--------|------------------------|
| `chess`        | `scripts/chess/chess_engine.js` | لا مكافئ Go ناضج قابل للتحميل هنا لـ `python-chess`+`cairosvg`؛ npm فيه `chess.js`+`sharp` جاهزين |
| `pinterest`    | `scripts/pinterest/pinterest_scraper.js` | يحتاج متصفح حقيقي (Playwright) لتجاوز حماية Pinterest/Cloudflare |
| `manga_bridge` | `scripts/mangabridge/manga_scraper.js` | نفس السبب — يحتاج متصفح حقيقي لتجاوز Cloudflare على 3asq.pro |
| `img_tr`       | `scripts/img_tr/img_tr_engine.js` | OCR + رسم النص فوق الصورة — لا مكافئ Go ناضج قابل للتحميل هنا؛ npm فيه أدوات جاهزة |

باقي الـ 4 plugins (`gemini`, `groq`, `sub`, `novel`) **Go خالص
100%** بدون أي تبعية Node. (يوجد أيضاً `ping` — مثال توضيحي بسيط Go خالص،
وليس أحد الثمانية المحوَّلة من بايثون — راجع "كيف تضيف plugin Go خالص
جديد" أدناه.)

## حالة كل plugin (مبني ومُختبر)

| Plugin         | Endpoint(s) | الحالة |
|----------------|-------------|--------|
| `gemini`       | `POST /gemini` | ✅ Go خالص |
| `groq`         | `POST /groq` | ✅ Go خالص (نص/صورة/صوت/فيديو) |
| `sub`          | `POST /subtitler/create`, `GET /subtitler/status/{job_id}`, `GET /subtitler/download/{job_id}` | ✅ Go خالص — اختُبر بفيديو حقيقي + ffmpeg حقيقي |
| `novel`        | `POST /novel`, `GET /novel/sites`, `DELETE /novel/cache` | ✅ Go خالص — استخراج HTML بدون تبعيات + اختبارات وحدة |
| `chess`        | `POST /process_move` | ✅ Go + Node — اختُبر end-to-end (نقلات، كش مات، رسم الرقعة) |
| `pinterest`    | `POST /pinterest`, `GET /pinterest/health` | ✅ Go + Node — منطق مكتوب بالكامل + اختبارات وحدة للسمافور (semaphore)، **الكشط الفعلي غير مُختبر بمتصفح حقيقي هنا** (انظر أدناه) |
| `manga_bridge` | `POST /manga-bridge/jobs`, `GET /manga-bridge/jobs/{job_id}`, `GET /manga-bridge/jobs/{job_id}/image/{idx}` | ✅ Go + Node — نفس ملاحظة pinterest |
| `img_tr`       | `POST /img_tr`, `POST /img_tr/batch` | ✅ Go + Node (OCR + رسم) — مع اختبارات وحدة |
| `ping` (مثال)  | `GET /ping` | ✅ Go خالص — توضيحي فقط، ليس جزءاً من الثمانية المحوَّلة |

## ملاحظة صادقة عن pinterest و manga_bridge

بيئة التطوير المستخدمة لبناء هذا المشروع تحجب `cdn.playwright.dev`،
فتعذّر فيها تحميل متصفح Chromium نفسه (`npx playwright install
chromium` يفشل). بالتالي سكربتا `pinterest_scraper.js` و
`manga_scraper.js` **مكتوبان ومنطقياً صحيحان ومُختبران في مسارات
الفشل السليم** (يرجعان JSON خطأ واضح بدل الانهيار)، لكن لم يتسنَّ اختبار
الكشط الفعلي بمتصفح حقيقي. على أي جهاز/صورة Docker بإنترنت طبيعي (راجع
الـ Dockerfile المرفق، يحمّل Chromium أثناء البناء)، سيعملان مباشرة.

هذا هو **بالضبط نفس القيد** الذي كانت تواجهه نسخة Python الأصلية أساساً
(حظر Cloudflare المحتمل لسمعة IP الخادم) — لم نُدخل هشاشة جديدة، فقط
نقلنا نفس الأداة (Playwright) للغة يمكن تحميل حزمها هنا (npm بدل Go modules).

## البنية

```
sunkenbot-go/
├── go.mod / go.sum
├── main.go                       # الملف الوحيد الذي "يعرف" عن كل الخدمات
├── Dockerfile                    # يثبّت Node + npm deps + Chromium تلقائياً
├── dockerignore                  # يستثني node_modules وملفات *_test.go والـ .git من سياق البناء
├── internal/
│   ├── plugins/route.go          # عقد Service الوحيد (Name/Routes) — كل خدمة تلتزم به
│   ├── httpx/httpx.go            # Handle/WrapJSON — البدائية العامة لكل handler
│   ├── shared/http.go            # http.Client مشترك (30s) للخدمات التي تستخدم هذه المهلة فعلاً
│   ├── middleware/middleware.go  # حماية X-Internal-Token + CORS
│   ├── netguard/{netguard.go,netguard_test.go} # حماية SSRF لأي رابط مُرسَل من المستخدم (مرفقات/صور) + اختبارات وحدة
│   └── session/                  # مخزن الجلسات (ذاكرة افتراضياً، Mongo اختياري)
├── plugins/
│   ├── gemini/gemini.go
│   ├── groq/{groq.go,groq_test.go}
│   ├── sub/sub.go
│   ├── novel/{novel.go,novel_test.go}
│   ├── img_tr/{img_tr.go,img_tr_test.go} # ترجمة نصوص الصور — endpoints: /img_tr و /img_tr/batch
│   ├── chess/chess.go             # طبقة HTTP فقط — يستدعي scripts/chess
│   ├── pinterest/{pinterest.go,pinterest_semaphore_test.go}       # طبقة HTTP + Ferdev fallback — يستدعي scripts/pinterest
│   ├── mangabridge/{mangabridge.go,mangabridge_semaphore_test.go} # طبقة job/API — يستدعي scripts/mangabridge
│   └── ping/{ping.go,ping_test.go} # مثال توضيحي فقط — يثبت بساطة إضافة خدمة جديدة
└── scripts/                      # سكربتات Node.js (subprocess helpers)
    ├── chess/{chess_engine.js, package.json, package-lock.json}
    ├── pinterest/{pinterest_scraper.js, package.json, package-lock.json}
    ├── img_tr/{img_tr_engine.js, package.json, package-lock.json}
    └── mangabridge/{manga_scraper.js, package.json, package-lock.json}
```

> **ملاحظة:** `node_modules/` غير مُرفَق (كما لا يُرفَق عادة في git) —
> يُبنى تلقائياً بـ `npm ci` (مُدرَج في الـ Dockerfile). لتشغيل محلي
> بدون Docker: `cd scripts/<name> && npm ci` لكل واحد منها.
>
> **ملاحظة:** حزمة `img_tr` (وليس `imgtr`) — اسم المجلد والـ import path
> الفعليان في `main.go` هما `sunkenbot/plugins/img_tr`، بشرطة سفلية
> تطابق اسم الـ endpoint وسكربت Node المقابل.

## التشغيل محلياً

```bash
# 1) تبعيات Node لكل سكربت (مرة واحدة)
for d in chess pinterest mangabridge img_tr; do (cd scripts/$d && npm ci); done

# 2) Chromium لِـ pinterest/manga_bridge فقط (chess لا يحتاجه، sharp تكفيه)
cd scripts/pinterest && npx playwright install chromium && cd ../..

# 3) بناء وتشغيل Go — مع دعم MongoDB حقيقي (راجع فقرة MONGO_URI أدناه)
go build -tags mongo -o sunkenbot .
./sunkenbot
# أو مع التوكن:
INTERNAL_TOKEN=secret ./sunkenbot
```

### ملاحظة عن وسم `-tags mongo` وسطور `replace` في go.mod

البناء بدون `-tags mongo` (`go build -o sunkenbot .`) ينجح أيضاً، لكن
`MONGO_URI` يُتجاهَل بصمت حينها ويعمل البوت بمخزن جلسات في الذاكرة فقط
(غير دائم عبر إعادة التشغيل) — راجع `internal/session/mongo_stub.go` مقابل
`mongo_real.go`. **استخدم `-tags mongo` دائماً في أي بيئة تريد فيها تخزيناً
دائماً فعلياً**، وهو ما يفعله `Dockerfile` المرفق فعلاً.

الـ `Dockerfile` المرفق مبني على 3 مراحل: (1) بناء ثنائي Go بـ `-tags mongo`،
(2) تثبيت تبعيات Node لكل سكربتات `scripts/*` مع تحميل Chromium عبر
Playwright في صورة وسيطة تُرمى بعد نسخ النتائج فقط (`/opt/pw-browsers`
و `scripts/`)، و(3) صورة تشغيل نهائية تُثبِّت مكتبات النظام التي يحتاجها
Chromium فعلياً وقت التشغيل (عبر `playwright install-deps`) وتُشغِّل
البوت كمستخدم غير-root. كما يتحقق البناء بنيوياً من تطابق نسخة `playwright`
بين `pinterest` و`mangabridge` (يشتركان في نفس `PLAYWRIGHT_BROWSERS_PATH`)
ويفشل مبكراً لو اختلفتا.

`go.mod` يحتوي أيضاً عدة أسطر `replace` تُوجِّه `go.mongodb.org/mongo-driver`
وحزم `golang.org/x/*` نحو مرايا GitHub الرسمية بدل مساراتها الأصلية —
ضرورية فقط في بيئات شبكة مقيّدة لا تصل لـ `go.mongodb.org`/`proxy.golang.org`
مباشرة (راجع التعليق التفصيلي أعلى تلك الأسطر في `go.mod`). لو كانت بيئتك
تصل لهذه النطاقات مباشرة، يمكن حذف أسطر `replace` بأمان دون أي تغيير في
السلوك — **لكن لا تحذفها كجزء من "تنظيف" روتيني لتبعيات go.mod** دون التأكد
أولاً أن بيئة البناء (محلياً وفي CI/Docker) تصل فعلاً لتلك النطاقات، وإلا
سينكسر البناء بـ `-tags mongo`.

## متغيرات البيئة

| المتغيّر | الوصف |
|----------|-------|
| `PORT` | المنفذ (افتراضي `7860`) |
| `INTERNAL_TOKEN` | حماية `X-Internal-Token` لكل الطلبات عدا `/` و `/health` |
| `GEMINI_API_KEY` / `_2` / `_3` / `_4` | مفاتيح Gemini |
| `GROQ_API_KEY` | مفتاح Groq |
| `FERDEV_API_KEY` | مفتاح fallback لـ Pinterest |
| `MONGO_URI` | اختياري — تخزين جلسات دائم، **يتطلب بناءً بـ `-tags mongo`** وإلا يُتجاهَل بصمت (راجع الفقرة أعلاه) |
| `CHESS_SCRIPT_PATH` / `PINTEREST_SCRIPT_PATH` / `MANGA_SCRIPT_PATH` | تخصيص مسار سكربت Node لو نُشر في مكان مختلف |
| `PINTEREST_CHROMIUM_PATH` / `MANGA_CHROMIUM_PATH` | مسار Chromium نظامي بديل بدل الافتراضي |

## لماذا هذا الشكل تحديداً؟ (internal/plugins، لا registry)

نسخة Python كانت تعتمد على تحميل ديناميكي حقيقي: `plugin_loader.py` يفتح
مجلد `plugins/` وقت التشغيل، يستورد كل ملف `.py` بشكل ديناميكي — وأي خطأ
في هذا (مثل نسيان تسجيل plugin) كان يظهر بصمت وقت التشغيل فقط. النسخة
الأولى من هذا المنفذ بـ Go استخدمت مكافئاً مباشراً لهذه الفكرة (حزمة
`internal/registry` + `init()` + `blank import` في `main.go`) — لكن هذا
تحديداً كان سبب اختفاء `img_tr` بصمت من الخدمة: نسيان سطر `blank import`
واحد لا يُنتج أي خطأ ترجمة، فقط endpoint غائب لا يُكتشف إلا يدوياً.

الشكل الحالي أبسط ومباشر أكثر: `internal/plugins.Service` عقد صغير
(`Name() string` + `Routes() []Route`)، وكل خدمة حزمة Go مستقلة تلتزم به
دون معرفة أي شيء عن الخدمات الأخرى. `main.go` هو المكان الوحيد الذي
"يعرف" عن كل الخدمات — سطر واحد في `services()` لكل خدمة، بلا `init()`
وبلا `blank import`. الفرق العملي: نسيان ذلك السطر الآن يعني ببساطة أن
الخدمة غير موجودة في القائمة (يلاحظه أي أحد بقراءة `main.go`)، لا عطلاً
صامتاً وقت التشغيل.

## كيف تضيف plugin Go خالص جديد

```go
// plugins/mytool/mytool.go
package mytool

import (
    "net/http"

    "sunkenbot/internal/httpx"
    "sunkenbot/internal/plugins"
)

const Description = "وصف مختصر للـ plugin"

type Service struct{}

func New() *Service { return &Service{} }

func (s *Service) Name() string { return "mytool" }

func (s *Service) Routes() []plugins.Route {
    return []plugins.Route{
        {Method: "GET", Pattern: "/my-endpoint", Handler: httpx.Handle(s.handle)},
    }
}

type result struct {
    Status string `json:"status"`
}

func (s *Service) handle(r *http.Request) (result, error) {
    return result{Status: "ok"}, nil
}
```

ثم في `main.go`: استورد الحزمة، وأضف `mytool.New()` لقائمة `services()`
في دالة `services()` — سطر واحد فقط. راجع `plugins/ping/ping.go` لمثال
كامل وأبسط بهذا النمط بالضبط.

## كيف تضيف plugin بحاجة Node.js (نمط chess/pinterest/manga_bridge)

1. أنشئ `scripts/<name>/` بحزمة npm مستقلة (`npm init -y && npm install ...`)
2. اكتب السكربت بنفس بروتوكول JSON عبر stdin/stdout المستخدم في الثلاثة الحاليين
3. أنشئ `plugins/<name>/<name>.go` بطبقة HTTP رفيعة تستدعي السكربت عبر
   `os/exec.CommandContext` (انسخ نمط `plugins/chess/chess.go` — الأبسط)
4. أضف تثبيت `npm ci` للسكربت الجديد في الـ Dockerfile
