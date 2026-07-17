# ============ مرحلة 1: بناء Go (خالص 100% — لا Node/npm بعد الآن) ============
#
# ملاحظة انحراف متعمَّد عن الطلب الأصلي (اقرأ قبل تغييره لـ
# golang:1.24-alpine مرة أخرى): طُلب هنا صراحة golang:1.24-alpine، لكن
# هذا غير متوافق فعلياً مع بقية المتطلبات في نفس الطلب. github.com/otiai10/gosseract/v2
# (رابط OCR الجديد في plugins/img_tr/ocr.go) هو رابط cgo حقيقي لمحرك
# Tesseract الفعلي، لا مكتبة Go خالصة — يحتاج CGO_ENABLED=1 ويرتبط
# ديناميكياً بـ libtesseract/libleptonica. أي ثنائي Go مبني بـ cgo على
# قاعدة Alpine (musl libc) مرتبط بمحمّل musl الديناميكي، ولا يمكن تشغيله
# إطلاقاً على صورة تشغيل بـ glibc مثل debian:bookworm-slim (نفس الصورة
# المطلوبة صراحة لمرحلة التشغيل أدناه، لتوفّر Chromium وTesseract بشكل
# أسهل وأكثر استقراراً هناك) — فشل فوري وقت exec بمحمّل ديناميكي غير
# متوافق، لا خطأ بناء واضح. لو أُبقي CGO_ENABLED=0 لتفادي هذا، يضيع OCR
# بالكامل، وهو لُبّ هذا الـ plugin. الإصلاح الوحيد الصحيح: توحيد قاعدة
# libc بين مرحلتي البناء والتشغيل — بما أن التشغيل يجب أن يبقى Debian،
# الحل هو بناء الثنائي على قاعدة Debian أيضاً (golang:1.24-bookworm بدل
# golang:1.24-alpine؛ نفس رقم إصدار Go 1.24 المطلوب بالضبط، فقط قاعدة
# نظام متوافقة فعلياً).
FROM golang:1.24-bookworm AS build
WORKDIR /app

# أدوات بناء + رؤوس Tesseract/Leptonica لازمة لربط cgo في gosseract.
# (الطلب الأصلي ذكر "musl-dev" — اسم حزمة خاص بـ Alpine تحديداً؛ المكافئ
# هنا على Debian هو build-essential.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential pkg-config \
        libtesseract-dev libleptonica-dev \
    && rm -rf /var/lib/apt/lists/*

COPY go.mod go.sum ./
RUN go mod download
COPY . .

# ملاحظة (من الملف الأصلي، لا تزال سارية بالكامل): سطور "replace" في
# go.mod التي توجّه go.mongodb.org/mongo-driver وحزم golang.org/x/*
# نحو مرايا GitHub ضرورية لبيئات شبكة مقيّدة لا تسمح بالوصول لـ
# go.mongodb.org/proxy.golang.org/golang.org مباشرة (راجع التعليق أعلى
# تلك الأسطر في go.mod) — لا تحذفها بحجة "تنظيف" التبعيات إلا لو تأكدت
# أن بيئة النشر لديك تصل لتلك النطاقات مباشرة.
#
# CGO_ENABLED=1 (لا 0 كما كان سابقاً قبل إضافة OCR): gosseract يحتاج cgo
# فعلياً — راجع الشرح الطويل أعلى هذا الملف. الثنائي الناتج بالتالي
# مرتبط ديناميكياً (ليس "static" بالمعنى الحرفي)، لذا مرحلة التشغيل
# *يجب* أن توفّر libtesseract/libleptonica وقت التشغيل — هذا ليس
# اختيارياً، راجع مرحلة 2 أدناه.
RUN CGO_ENABLED=1 GOOS=linux go build -tags mongo -trimpath -ldflags="-s -w" -o /sunkenbot .

# ============ مرحلة 2: التشغيل ============
FROM debian:bookworm-slim

# حِزم وقت التشغيل:
#   - stockfish: محرك الشطرنج (plugins/chess عبر UCI subprocess — لا علاقة له بـ Node)
#   - chromium: راجع internal/browser — يستبدل Playwright/Node بالكامل
#     لـ plugins/pinterest وplugins/mangabridge (chromedp يتحدث معه
#     مباشرة عبر CDP، بلا أي وسيط)
#   - ffmpeg: يُستخدَم من plugins/sub وplugins/groq لمعالجة الوسائط (غير
#     متعلق بهذا الترحيل، أُبقي كما كان)
#   - tesseract-ocr + حزم اللغات: نفس القائمة بالضبط التي كانت
#     LANG_TO_TESSERACT تفترض توفّرها في img_tr_engine.js السابق (Node) —
#     الآن ملفات تدريب حقيقية على القرص بدل افتراض توفّرها عبر تنزيل
#     tesseract.js التلقائي وقت التشغيل.
#   - fonts-noto-core: يوفّر NotoSansArabic-Regular.ttf الذي يحمّله
#     plugins/img_tr/render.go مباشرة لرسم النص العربي المترجَم (نفس
#     الحزمة بالضبط التي كانت مثبَّتة سابقاً من أجل Pango/librsvg —
#     الفرق الآن أننا نحمّل ملف TTF منها مباشرة بدل المرور عبر fontconfig).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        stockfish \
        chromium \
        ffmpeg \
        tesseract-ocr \
        tesseract-ocr-eng tesseract-ocr-jpn tesseract-ocr-kor \
        tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
        tesseract-ocr-spa tesseract-ocr-por tesseract-ocr-fra tesseract-ocr-deu \
        tesseract-ocr-ita tesseract-ocr-rus tesseract-ocr-ind tesseract-ocr-vie \
        tesseract-ocr-tha tesseract-ocr-pol tesseract-ocr-tur tesseract-ocr-nld \
        tesseract-ocr-ukr tesseract-ocr-ell tesseract-ocr-ara \
        fonts-noto-core fonts-noto-ui-core fonts-dejavu-core fontconfig \
        librsvg2-bin \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r sunkenbot && useradd -r -g sunkenbot -d /app sunkenbot

# ملاحظة إصلاح (من الملف الأصلي، لا تزال سارية): حزمة stockfish من
# مستودعات Debian bookworm تُثبَّت في /usr/games لا /usr/bin،
# وplugins/chess/chess.go يبحث عن "stockfish" عبر PATH الافتراضي.
# /usr/games *غير* مُدرَج ضمن PATH الافتراضي لعمليات exec داخل حاوية
# (ذلك الإدراج يحصل فقط لجلسات الدخول التفاعلية عبر /etc/login.defs) —
# بدون هذا السطر: "executable file not found in $PATH" عند كل رد آلي.
ENV PATH="${PATH}:/usr/games"

WORKDIR /app
COPY --from=build /sunkenbot /app/sunkenbot

# مجلد صور مانجا-بريدج (plugins/mangabridge يكتب هنا مباشرة — راجع
# imagesSubdir في mangabridge.go) — يُنشأ صراحة هنا بملكية sunkenbot بدل
# الاعتماد على os.MkdirAll وقت التشغيل لإنشائه بصلاحيات المستخدم
# الصحيحة من أول تشغيل.
RUN mkdir -p /app/data/manga_bridge/images && chown -R sunkenbot:sunkenbot /app

USER sunkenbot

# غير-root آمن هنا فعلياً (لا مجرد افتراض): internal/browser/browser.go
# يمرّر chromedp.NoSandbox دائماً بلا شرط لكل جلسة Chromium (راجع ذلك
# الملف) — تماماً كما كانت pinterest_scraper.js/manga_scraper.js تمرران
# --no-sandbox دائماً بلا شرط سابقاً. لا حاجة لأي قدرة (capability)
# إضافية على مستوى الحاوية.
EXPOSE 7860
ENV PORT=7860
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1
CMD ["/app/sunkenbot"]
