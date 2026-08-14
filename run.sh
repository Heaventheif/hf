#!/bin/sh
# run.sh — المشغّل الموحّد: يرفع Python (s) على منفذ داخلي ثم Go (g).
#
# لماذا شل سكربت بسيط بدل supervisord؟
#   1. لا حاجة لأي تعقيد: عملية واحدة خلفية (uvicorn) + عملية أمامية (go).
#   2. عند توقف Go (خطأ قاتل) — وهو ما يكتشفه HEALTHCHECK — تتوقف الحاوية
#      كلها تلقائياً (PID 1 يموت) وهذا هو السلوك الصحيح: بدون البوابة
#      الخارجية لا معنى لبقاء Python وحدها.
#   3. لا اعتماديات إضافية ولا ملفات config منفصلة.
#
# ترتيب البدء: Python أولاً لأن Go يحتاجها منذ أول طلب (HealthCheck بعد
# 3 ثوانٍ). إذا تعذر رفع Python (مثلاً HF_PYTHON_URL غير صالح) يستمر Go
# ويُسجِّل تحذيراً واضحاً — المسارات المحلية تعمل والموكلة تعطي 502 مع
# رسالة خطأ مفهومة.

set -e

# ─── Python: 8001 داخلي فقط ────────────────────────────────────────────────
# Python لا ترى INTERNAL_TOKEN إلا لتحمي نفسها — نفس التوكن الذي تستخدمه Go
# عندما تتكلم معها، لكن من الخارج لا ينجح أحد في الوصول لمنفذها لأنه غير
# مكشوف أصلاً (EXPOSE 7860 فقط).
uvicorn main:app \
    --app-dir /app/python-services \
    --host 127.0.0.1 \
    --port 8001 \
    --workers 1 \
    --log-level info > /tmp/python.log 2>&1 &
PY_PID=$!

# ─── Go: البوابة الخارجية ──────────────────────────────────────────────────
# HF_PYTHON_URL داخلي دائماً — localhost:8001 بلا TLS (أسرع وأبسط).
# لا تغيّر هذه القيمة إلا لو شغّلت Python على آلة منفصلة فعلاً.
export HF_PYTHON_URL="${HF_PYTHON_URL:-http://localhost:8001}"

# trap: عند توقف Go لأي سبب ننهي Python معه حتى لا تظل معلقة وحدها.
cleanup() { kill $PY_PID 2>/dev/null; wait $PY_PID 2>/dev/null; }
trap cleanup EXIT

# انتظار Python حتى تصبح جاهزة (uvicorn يكتب "Application startup complete"
# في اللوق) — مع حد أقصى 60 ثانية حتى لا نعلق أبداً.
for i in $(seq 1 60); do
    if grep -q "Application startup complete" /tmp/python.log 2>/dev/null; then
        echo "✅ [run] خدمة Python جاهزة على المنفذ 8001 (داخلي)"
        break
    fi
    # فشل مبكر واضح بدل صمت: لو ماتت uvicorn أثناء البدء نطبع آخر 10 سطور
    if ! kill -0 "$PY_PID" 2>/dev/null; then
        echo "❌ [run] تعذر تشغيل خدمة Python — آخر اللوق:" >&2
        tail -n 10 /tmp/python.log >&2
        exit 1
    fi
    sleep 1
done

# ─── Go إلى الأمام (يحجز المنفذ 7860) ─────────────────────────────────────
exec /app/sunkenbot
