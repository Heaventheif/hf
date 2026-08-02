---
title: HF Manga Translate
emoji: 📖
colorFrom: blue
colorTo: purple
sdk: docker
app_file: app.py
pinned: false
---

# hf-manga-translate

OCR + translation API for manga pages. Extracts text via PaddleOCR and translates to Arabic using NLLB-200.

## API

**POST** `/infer`

```json
{
  "image_urls": ["https://example.com/page1.jpg"]
}
```
