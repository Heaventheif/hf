FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# تثبيت الاعتماديات النظامية + Chromium (كامل) لتشغيل nodriver و Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    fonts-noto-core \
    fonts-noto-ui-core \
    # Playwright / Chromium runtime deps
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangoft2-1.0-0 \
    libxshmfence1 \
    libx11-xcb1 \
    # Chromium browser (used by nodriver)
    chromium \
    chromium-common \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# تثبيت متطلبات Python (بما فيها متطلبات المتصفح)
COPY requirements_browser.txt .
RUN pip install --no-cache-dir -r requirements_browser.txt

# تثبيت Playwright ومتصفحه (للتوافق مع بقية النظام)
RUN pip install playwright \
    && playwright install chromium \
    && playwright install-deps chromium

# نسخ باقي الملفات (سيشمل لاحقاً main.py و plugins...)
COPY . .

EXPOSE 7860

# في حال أردت تشغيل تطبيق FastAPI الرئيسي:
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]