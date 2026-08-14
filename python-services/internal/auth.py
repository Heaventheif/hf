"""
internal/auth.py — حماية X-Internal-Token مطابقة لـ middleware/middleware.go في Go.

القاعدة الواحدة: كل endpoint محمي ما عدا GET / و GET /health.
إن كان INTERNAL_TOKEN فارغاً، تُفتح كل الـ endpoints مع تحذير في اللوق —
نفس سلوك Go حرفياً.
"""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger(__name__)

# مسارات مستثناة من الحماية — تقابل publicPaths في Go
_PUBLIC = {"/", "/health", "/ping"}


def _tokens_match(a: str, b: str) -> bool:
    """مقارنة بزمن ثابت لمنع timing side-channel — تقابل subtle.ConstantTimeCompare."""
    return hmac.compare_digest(a.encode(), b.encode())


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware يفحص X-Internal-Token على كل endpoint غير عام.
    يُنشأ مرة واحدة عند بدء التطبيق ويُمرَّر كـ middleware لـ FastAPI.
    """

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token.strip()

        if not self._token:
            log.warning(
                "⚠️  INTERNAL_TOKEN غير مضبوط — كل الـ endpoints مفتوحة بدون حماية! "
                "أضف INTERNAL_TOKEN في متغيرات البيئة."
            )
        else:
            log.info("🔒 تم تفعيل حماية X-Internal-Token على كل الـ endpoints (عدا / و /health و /ping)")

    async def dispatch(self, request: Request, call_next):
        # بدون توكن → لا حماية (نفس سلوك Go)
        if not self._token:
            return await call_next(request)

        # المسارات العامة لا تحتاج توكن
        if request.url.path in _PUBLIC:
            return await call_next(request)

        supplied = request.headers.get("X-Internal-Token", "")
        if not _tokens_match(supplied, self._token):
            log.warning(
                "🚫 طلب مرفوض (توكن غير صحيح/مفقود) — %s %s من %s",
                request.method,
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                status_code=401,
                content={"status": "error", "message": "Unauthorized — missing or invalid X-Internal-Token"},
            )

        return await call_next(request)


def make_auth_middleware(app) -> TokenAuthMiddleware:
    """نقطة الدخول الوحيدة — يقرأ INTERNAL_TOKEN من البيئة."""
    token = os.environ.get("INTERNAL_TOKEN", "")
    return TokenAuthMiddleware(app, token)
