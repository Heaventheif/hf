---
title: Sunken python serveces
emoji: 📖
colorFrom: blue
colorTo: purple
sdk: docker
app_file: app.py
pinned: false
---
# SunkenBot — Python Services

خدمة ثانوية تعمل على Hugging Face Docker Space.  
تتلقى طلباتها من **Go (g) فقط** — لا تتصل بها Render مباشرة.

```
Render (xx) ──► Go (g) ──[X-Internal-Token]──► Python (s)
```

---

## هيكل المشروع

```
s/
├── main.py                   ← نقطة الدخول — لا تُعدَّل
├── collect_requirements.py   ← يجمع requirements من plugins — لا تُعدَّل
├── requirements.base.txt     ← FastAPI + uvicorn فقط — لا تُعدَّل
├── requirements.txt          ← مولَّد تلقائياً — لا تُعدَّل يدوياً
├── Dockerfile                ← لا تُعدَّل
├── internal/
│   ├── plugin.py             ← عقد Plugin (ABC) — لا تُعدَّل
│   ├── loader.py             ← اكتشاف ديناميكي — لا تُعدَّل
│   └── auth.py               ← حماية X-Internal-Token — لا تُعدَّل
└── plugins/
    ├── ocr.py                ← plugin OCR + ترجمة
    ├── ping.py               ← plugin فحص الاتصال
    └── your_plugin.py        ← ← plugin جديد هنا فقط
```

**القاعدة الوحيدة:** إضافة plugin = ملف جديد في `plugins/` فقط.  
لا تعديل في أي ملف آخر.

---

## إضافة Plugin جديد — خطوة بخطوة

### 1 — أنشئ الملف

```
plugins/my_feature.py
```

اسم الملف = اسم الـ plugin في اللوق وفي `GET /`.  
يجب أن يبدأ بحرف (لا `_`) وإلا يُتجاوَز تلقائياً.

---

### 2 — الهيكل الكامل الإلزامي

```python
# plugins/my_feature.py
from __future__ import annotations

from internal.plugin import Plugin, Route

# ─── Handlers ────────────────────────────────────────────────────────────────
# دوال FastAPI عادية — sync أو async، بأي توقيع تقبله FastAPI.

async def _handle_something(req: MyRequest) -> MyResponse:
    ...


# ─── Plugin definition ────────────────────────────────────────────────────────

class MyFeaturePlugin(Plugin):
    description = "وصف مختصر يظهر في GET /"   # اختياري

    # حزم pip التي يحتاجها هذا plugin فقط.
    # يقرأها collect_requirements.py وقت بناء Docker — لا تضعها في requirements.txt يدوياً.
    requirements = [
        "requests",
        "some-library>=1.2",
    ]

    # سطور pip خاصة (--extra-index-url, --find-links ...).
    # تظهر قبل requirements في requirements.txt المولَّد.
    # اتركها [] إن لم تكن بحاجة لها.
    pip_extra = []

    def name(self) -> str:
        return "my_feature"          # فريد، بدون مسافات أو شرطات

    def routes(self) -> list[Route]:
        return [
            Route(method="POST", path="/my_feature/action", handler=_handle_something),
        ]

    # ─── خطافات اختيارية ──────────────────────────────────────────────────

    def startup(self) -> None:
        """يُنفَّذ مرة عند بدء التطبيق — استخدمه لتحميل نماذج ثقيلة."""
        pass

    def shutdown(self) -> None:
        """يُنفَّذ عند إيقاف التطبيق — استخدمه لتحرير الموارد."""
        pass

    def status(self) -> dict:
        """يظهر في GET / — أرجع ما يفيد في مراقبة الحالة."""
        return {"status": "loaded"}


# ─── السطر الأهم — يجب أن يكون في نهاية الملف ────────────────────────────────
plugin = MyFeaturePlugin()
```

---

### 3 — القواعد الإلزامية (يُرفَض plugin يخالفها صامتاً)

| القاعدة | التفاصيل |
|---|---|
| **`plugin = MyPlugin()`** | متغير اسمه `plugin` بالضبط في آخر الملف — الـ loader يبحث عنه فقط |
| **يرث من `Plugin`** | `from internal.plugin import Plugin` ثم `class X(Plugin):` |
| **`name()` لا يتكرر** | إن وجد pluginان بنفس `name()` يُحمَّل الأول أبجدياً ويُتجاوَز الثاني |
| **`routes()` يعيد قائمة** | حتى لو route واحد — `return [Route(...)]` لا `return Route(...)` |
| **مسارات لا تتعارض** | `/ping` و`/health` و`/` محجوزة للنظام — لا تستخدمها |
| **`requirements` literals فقط** | قيم نصية ثابتة في الكود — لا متغيرات ولا f-strings (يُقرأ كـ AST) |
| **اسم الملف لا يبدأ بـ `_`** | `_helper.py` يُتجاوَز تلقائياً — مناسب للملفات المساعدة |

---

### 4 — قواعد `requirements` و`pip_extra`

```python
# ✅ صحيح — literals نصية مباشرة
requirements = [
    "requests",
    "pillow>=9.0",
    "torch",
]
pip_extra = [
    "--extra-index-url https://download.pytorch.org/whl/cpu",
]

# ❌ خطأ — collect_requirements.py يقرأ AST ولا يرى المتغيرات
MY_LIBS = ["requests", "pillow"]
requirements = MY_LIBS          # لن يُقرأ

# ❌ خطأ — f-string لا يُقرأ كـ AST
requirements = [f"torch=={TORCH_VERSION}"]
```

**تكرار الحزم:** إن أعلن pluginان عن نفس الحزمة، يُأخذ إعلان الأول أبجدياً ويُتجاوَز الثاني.  
إن احتجت version مختلفة في plugin واحد — حدّد الـ version specifier فيه أنت.

---

### 5 — أنواع الـ handlers المقبولة

```python
from pydantic import BaseModel
from fastapi import HTTPException

# نوع 1 — POST بجسم JSON (الأكثر شيوعاً)
class MyRequest(BaseModel):
    text: str
    lang: str = "ar"

class MyResponse(BaseModel):
    result: str

async def _handle(req: MyRequest) -> MyResponse:
    if not req.text:
        raise HTTPException(status_code=400, detail="text مطلوب")
    return MyResponse(result=req.text.upper())

Route(method="POST", path="/my/endpoint", handler=_handle)


# نوع 2 — GET بدون جسم
async def _handle_info() -> dict:
    return {"version": "1.0"}

Route(method="GET", path="/my/info", handler=_handle_info)


# نوع 3 — عمليات CPU ثقيلة (لا تبلوك event loop)
import asyncio

def _heavy_cpu_work(data: str) -> str:
    ...  # عملية بطيئة

async def _handle_heavy(req: MyRequest) -> MyResponse:
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _heavy_cpu_work, req.text)
    return MyResponse(result=result)
```

---

### 6 — النماذج الثقيلة (ML models)

```python
# ✅ النمط الصحيح: lazy singleton + تحميل مسبق في startup()

_model = None   # متغير على مستوى الوحدة — يعيش طوال عمر التطبيق

def _get_model():
    global _model
    if _model is None:
        _model = load_my_model()   # يُنفَّذ مرة واحدة فقط
    return _model

class MyPlugin(Plugin):
    def startup(self) -> None:
        # يُشغَّل عند بدء التطبيق — يُحمَّل النموذج مسبقاً
        # بدون هذا: أول طلب حقيقي يعاني من تأخير التحميل
        try:
            _get_model()
        except Exception as exc:
            log.warning("[my_plugin] تعذّر التحميل المسبق: %s", exc)
            # لا ترفع استثناء هنا — التطبيق يكمل بدون النموذج

    def status(self) -> dict:
        return {
            "status": "loaded",
            "model_ready": _model is not None,
        }
```

---

### 7 — مثال كامل: plugin بسيط

```python
# plugins/translate.py
from __future__ import annotations
import logging
from pydantic import BaseModel
from fastapi import HTTPException
from internal.plugin import Plugin, Route

log = logging.getLogger(__name__)


class TranslateRequest(BaseModel):
    text: str
    target_lang: str = "ar"

class TranslateResponse(BaseModel):
    translated: str
    target_lang: str


async def _handle_translate(req: TranslateRequest) -> TranslateResponse:
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text لا يمكن أن يكون فارغاً")
    # ... منطق الترجمة
    translated = req.text  # placeholder
    return TranslateResponse(translated=translated, target_lang=req.target_lang)


class TranslatePlugin(Plugin):
    description = "ترجمة نصوص"
    requirements = ["deep-translator>=1.11"]
    pip_extra    = []

    def name(self) -> str:
        return "translate"

    def routes(self) -> list[Route]:
        return [
            Route(method="POST", path="/translate", handler=_handle_translate),
        ]


plugin = TranslatePlugin()
```

---

## متغيرات البيئة

| المتغير | مطلوب | الوصف |
|---|---|---|
| `INTERNAL_TOKEN` | ✅ | التوكن المشترك مع Go — كل طلب يجب أن يحمله في `X-Internal-Token` |
| `PORT` | لا | المنفذ (افتراضي: `7860`) |

> **ملاحظة:** إن كان `INTERNAL_TOKEN` فارغاً، تُفتح كل الـ endpoints بدون حماية مع تحذير في اللوق.  
> المسارات `/`, `/health`, `/ping` مستثناة دائماً من الحماية.

---

## دورة بناء Docker

```
Dockerfile
  │
  ├── COPY requirements.base.txt + internal/ + plugins/ + collect_requirements.py
  │
  ├── RUN python collect_requirements.py
  │       └── يقرأ plugins/*.py كـ AST
  │           └── يجمع requirements + pip_extra من كل plugin
  │               └── يكتب requirements.txt
  │
  ├── RUN pip install -r requirements.txt
  │
  └── COPY main.py → CMD uvicorn main:app
```

**لماذا AST وليس import؟**  
`collect_requirements.py` يعمل قبل `pip install` — أي أن مكتبات plugins غير مثبتة بعد.  
القراءة كـ AST تسمح باستخراج `requirements` و`pip_extra` دون تشغيل الكود.

---

## أخطاء شائعة

| الخطأ | السبب | الحل |
|---|---|---|
| Plugin لا يظهر في `GET /` | `plugin = MyPlugin()` غائب أو خطأ في الاستيراد | تحقق من اللوق عند بدء التطبيق |
| `requirements` لا تُجمَع | استخدام متغير أو f-string بدل literal | استخدم نصوصاً ثابتة فقط |
| تعارض في المسارات | pluginان بنفس الـ path | غيّر path أحدهما |
| بطء في أول طلب | نموذج يُحمَّل عند الطلب لا عند البدء | أضف التحميل في `startup()` |
| `501 Unauthorized` | `X-Internal-Token` غائب أو خاطئ | تأكد من إرساله في كل طلب من Go |
