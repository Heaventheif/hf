---
title: YouTube Arabic AI Dubbing
emoji: 🎙️
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# YouTube Arabic AI Dubbing

خادم دبلجة عربية لحظية من الصوت الملتقط من تبويب YouTube عبر WebSocket، مع امتداد MV3 في مجلد `extension/`.

## المكونات

- **STT:** faster-whisper (tiny افتراضيًا، ويمكن اختيار base/small عبر `WHISPER_MODEL`).
- **الترجمة:** NLLB-200 CTranslate2 int8.
- **TTS:** Piper `ar_JO-kareem-medium`، ويُحمّل مرة واحدة عند بدء الخادم.
- **النقل:** PCM16 mono 16 kHz من الإضافة إلى `/ws`، وPCM16 بمعدل Piper من الخادم.

## تشغيل Space

يُبنى Dockerfile النماذج داخل الصورة. بعد التشغيل:

- `GET /health`
- `GET /voices`
- `wss://<space-subdomain>.hf.space/ws`

## تثبيت الإضافة

1. نزّل مجلد `extension/` كاملًا.
2. فعّل وضع المطور في متصفح Chromium الذي يدعم الإضافات.
3. اختر **Load unpacked** وحدد مجلد `extension/`.
4. افتح `https://m.youtube.com` أو YouTube في تبويب مدعوم، ثم اضغط أيقونة الإضافة واضغط **Enable dubbing**.
5. لا يحتاج اتصال الدبلجة إلى إعداد اعتماد إضافي.

## قيود مهمة

- `tabCapture` يحتاج نقرة مباشرة من المستخدم.
- Chrome الرسمي على Android لا يدعم تثبيت إضافات سطح المكتب عادةً؛ استخدم متصفح Android يدعم MV3/الإضافات إن كان متاحًا، أو Chrome/Edge على سطح المكتب. دعم `tabCapture` و`offscreen` على الهاتف يعتمد على المتصفح.
- الدبلجة متأخرة عن الفيديو بسبب المعالجة (Live Lag)، ولا يمكنها أن تسبقه.
- محتوى DRM قد يكون صامتًا، والإعلانات تُتجاهل عند اكتشاف `.ad-showing`.
- النموذج العربي الرسمي المتاح هنا هو Kareem فقط.
- Space المجاني قد ينام أو يبطئ؛ استخدم زر إيقاظ الخادم من الإضافة.
- المستخدم مسؤول عن احترام شروط YouTube وحقوق المحتوى.

## الترخيص

كود المشروع MIT. Piper محرك GPL-3.0؛ راجع ترخيصه عند إعادة التوزيع. نماذج الصوت والترجمة وWhisper لها تراخيصها الخاصة.
