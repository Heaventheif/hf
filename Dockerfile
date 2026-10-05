# Python 3.12: هو الإصدار الذي جرى التحقق من تثبيت هذه الحزم عليه (انظر README).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    MODELS_DIR=/home/user/models
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# النماذج تُنزَّل وقت البناء (طبقة مستقلة حتى لا تُعاد عند تعديل الكود)
COPY --chown=user config.py download_models.py ./
ARG WHISPER_MODELS="tiny base small"
ENV WHISPER_MODELS=${WHISPER_MODELS}
RUN python download_models.py

COPY --chown=user . .

EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860", "--ws", "websockets", "--ws-ping-interval", "20", "--ws-ping-timeout", "30"]
