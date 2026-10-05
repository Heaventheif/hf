# امتداد YouTube Arabic AI Dubbing

هذا امتداد Manifest V3 تجريبي يلتقط صوت تبويب YouTube عبر `tabCapture`، يرسله إلى Space، ويشغّل الصوت العربي في Offscreen Document حتى لا يحدث loop.

## الهاتف

Chrome Android الرسمي لا يتيح عادةً تثبيت إضافات سطح المكتب. جرّب متصفحًا مبنيًا على Chromium ويدعم MV3 و`tabCapture`/`offscreen`، أو استخدم Chrome/Edge على سطح المكتب. لا يمكن للامتداد تجاوز قيود المتصفح.

## التثبيت

استخدم **Load unpacked** على مجلد `extension/`. رابط Space الافتراضي هو `https://kiyunhai-s.hf.space` ويمكن تغييره من Popup. الاتصال مفتوح ولا يحتاج إعداد اعتماد إضافي.

## الاختبار

1. افتح YouTube وتأكد من ظهور فيديو مع عنصر `video`.
2. اضغط أيقونة الامتداد ثم **تشغيل الدبلجة**.
3. تحقق من `/health` أولًا إذا ظهر `loading` وانتظر حتى تصبح النماذج جاهزة.
4. راقب حالة الاتصال من Popup، ثم اختبر pause وseek وتغيير الفيديو.
