"""
internal/plugin.py — عقد Plugin الوحيد الذي يلتزم به كل plugin في هذا المشروع.

يقابل plugins/route.go في Go:
  - Plugin.name()   →  Service.Name()
  - Plugin.routes() →  Service.Routes()
  - Route           →  plugins.Route

القاعدة: كل ملف في plugins/ يُصدِّر متغيراً اسمه `plugin`
من نوع Plugin (أو أي كلاس يرث منه). هذا كافٍ — لا registry،
لا decorators، لا import سحري.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Route:
    """
    مسار واحد: method + path + handler.
    handler: دالة FastAPI عادية (sync أو async) — تُضاف مباشرة لـ APIRouter.
    """
    method: str          # "GET" | "POST" | ...
    path: str            # "/ocr/infer"
    handler: Callable    # دالة FastAPI


class Plugin(ABC):
    """
    العقد الوحيد — أي class يرث من Plugin ويُنفِّذ name() و routes()
    يصبح plugin صالحاً تلقائياً.

    description: وصف اختياري يظهر في GET /
    requirements: قائمة حزم pip التي يحتاجها هذا Plugin.
                  يقرأها collect_requirements.py تلقائياً عند بناء الـ image.
                  الصيغة مطابقة لسطر في requirements.txt:
                    ["requests>=2.28", "pillow"]
                  سطور pip الخاصة (مثل --extra-index-url) تُضاف في
                  pip_extra كقائمة منفصلة وتظهر قبل الحزم في الملف النهائي.
    pip_extra:    سطور pip إضافية تسبق requirements هذا الـ plugin
                  (مثل --extra-index-url, --find-links, -f ...).
    """

    description: str = ""
    requirements: list[str] = []
    pip_extra: list[str] = []

    @abstractmethod
    def name(self) -> str:
        """اسم Plugin — يظهر في GET / وفي اللوق."""

    @abstractmethod
    def routes(self) -> list[Route]:
        """قائمة المسارات التي يوفرها هذا Plugin."""

    def startup(self) -> None:
        """
        خطاف اختياري: يُنفَّذ مرة واحدة عند بدء التطبيق (lifespan).
        استخدمه لتحميل نماذج ثقيلة مسبقاً (warmup) بدل lazy loading.
        """

    def shutdown(self) -> None:
        """خطاف اختياري: تنظيف عند إيقاف التطبيق."""

    def status(self) -> dict[str, Any]:
        """
        حالة اختيارية تظهر في GET / — يمكن override لإرجاع معلومات مثل
        {'model_loaded': True, 'device': 'cpu'}.
        """
        return {"status": "loaded"}
