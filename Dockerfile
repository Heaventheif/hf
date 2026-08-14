# ═══════════════════════════════════════════════════════════════════════════
#  SunkenBot — الفضاء الموحّد (Go + Python في حاوية واحدة)
# ═══════════════════════════════════════════════════════════════════════════
#
# البنية بعد هذا الملف (بدل فضاءين منفصلين kiyunhai + kiyunhai-s):
#
#   Render ──► Go (g)        : المنفذ 7860 — نفس HF_SPACE_URL القديم حرفياً
#                ├─ endpoints السريعة تُحسب محلياً في Go:
#                │    gemini, groq, comic, novel, sub, manga-bridge,
#                │    pinterest, ping
#                └─ endpoints الثقيلة تُوَّكل (proxy خام) إلى Python:
#                     └─ Python (s): المنفذ 8001 — داخلي على localhost فقط
#                        لا يراه أحد من الخارج
#                        (chess, dama, ocr)
#
#  SunkenBot لا يعلم بهذا التقسيم إطلاقاً — يكلّم منفذاً واحداً
#  (HF_SPACE_URL) والمسارات كلها كما كانت تماماً.
#
#  متغيرات البيئة:
#    HF_SPACE_URL      — رابط الفضاء الخارجي (يستخدمه SunkenBot فقط)
#    INTERNAL_TOKEN    — توكن الحماية بين Render↔Go و Go↔Python
#    HF_PYTHON_URL     — يُصدَّر تلقائياً من run.sh (http://localhost:8001)
#                        لا حاجة لضبطه يدوياً أبداً.
#
# ═══════════════════════════════════════════════════════════════════════════

# ════ المرحلة 1: بناء Go ═══════════════════════════════════════════════════
FROM golang:1.26-alpine AS build
WORKDIR /app
RUN apk add --no-cache git ca-certificates
COPY go.mod go.sum ./
COPY . .
# go mod tidy داخل مرحلة البناء (كما كان في الأصل): حزمة جديدة في أي
# import يُولَّد لها سطر go.sum صحيح تلقائياً وقت البناء في بيئة HF.
RUN go mod tidy
RUN CGO_ENABLED=0 GOOS=linux go build -tags postgres -trimpath -ldflags="-s -w" -o /sunkenbot .

# ════ المرحلة 2: التشغيل الموحّد ═══════════════════════════════════════════
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl python3 python3-pip \
        # ─── مطلوبة لـ Go services (نفس Dockerfile القديم حرفياً) ───────
        chromium ffmpeg \
        # ─── مطلوبة لـ Python services (نفس Dockerfile s القديم حرفياً) ──
        libgl1 libglib2.0-0 \
        libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-xlib-2.0-0 \
        stockfish \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r sunkenbot && useradd -r -g sunkenbot -d /app sunkenbot

# stockfish من مستودعات Debian تُثبَّت في /usr/games غير المدرج في PATH
# الافتراضي — نفس ملاحظة إصلاح الملف القديم (لا تحذفها).
ENV PATH="${PATH}:/usr/games"

WORKDIR /app

# ─── Python services: نفس خطوات s القديمة حرفياً (للاستفادة من طبقات cache)
COPY python-services/requirements.base.txt python-services/
COPY python-services/internal/ python-services/internal/
COPY python-services/plugins/ python-services/plugins/
COPY python-services/collect_requirements.py python-services/
WORKDIR /app/python-services
RUN python3 collect_requirements.py && pip install --no-cache-dir -r requirements.txt

# ─── Go binary من مرحلة البناء ────────────────────────────────────────────
COPY --from=build /sunkenbot /app/sunkenbot

# ─── orchestrator: يرفع Python على 8001 داخلياً ثم Go على 7860 ────────────
WORKDIR /app
COPY run.sh .
RUN chmod +x run.sh

# مجلد صور manga-bridge (plugins/mangabridge يكتب هنا) — نفس الملف القديم
RUN mkdir -p /app/data/manga_bridge/images && chown -R sunkenbot:sunkenbot /app

USER sunkenbot
# غير-root آمن: internal/browser يمرر chromedp.NoSandbox دائماً.

EXPOSE 7860
ENV PORT=7860
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1
CMD ["/app/run.sh"]
