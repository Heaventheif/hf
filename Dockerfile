FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    # ─── إضافات مطلوبة لـ chess plugin ──────────────────────────────
    # Cairo: تحويل SVG → PNG (يستخدمها cairosvg)
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf2.0-0 \
    # Stockfish: محرك الشطرنج (fallback إذا فشل Lichess Cloud)
    stockfish \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ─── نسخ ما يحتاجه collect_requirements.py فقط ───────────────────────
# (قبل pip install لاستغلال Docker layer cache)
COPY requirements.base.txt .
COPY internal/ ./internal/
COPY plugins/ ./plugins/
COPY collect_requirements.py .

# ─── جمع requirements من plugins وكتابة requirements.txt ─────────────
RUN python collect_requirements.py

# ─── تثبيت الحزم المجموعة ────────────────────────────────────────────
RUN pip install --no-cache-dir -r requirements.txt

# ─── نسخ بقية الملفات ────────────────────────────────────────────────
COPY main.py .

EXPOSE 7860

CMD ["uvicorn", "main:app", "--host=0.0.0.0", "--port=7860"]
