FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models/huggingface \
    WHISPER_MODEL_DIR=/models/whisper \
    TRANSLATION_MODEL_DIR=/models/nllb \
    PIPER_MODEL_DIR=/models/piper \
    PORT=7860

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl espeak-ng libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY download_models.py .
RUN python download_models.py

COPY app.py config.py segmenter.py stt.py translator.py tts.py ./
COPY extension ./extension
COPY README.md ./README.md

RUN useradd --create-home --uid 1000 user \
    && chown -R user:user /app /models
USER user

EXPOSE 7860
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860}"]
