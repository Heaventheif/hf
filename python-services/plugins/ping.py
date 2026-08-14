"""
plugins/ping.py — أبسط plugin ممكن: GET /ping/echo

يُظهر كيف تبدو بنية plugin جديد من الصفر:
  1. import Plugin و Route من internal.plugin
  2. كتابة handler عادي
  3. تعريف class يرث Plugin
  4. متغير `plugin` في آخر الملف

هذا كل شيء — لا تعديل في main.py ولا في أي ملف آخر.
"""
from internal.plugin import Plugin, Route


async def _handle_echo() -> dict:
    return {"echo": "pong", "service": "sunkenbot-python"}


class PingPlugin(Plugin):
    description = "فحص بسيط للاتصال"
    requirements = []  # لا يحتاج حزم إضافية

    def name(self) -> str:
        return "ping"

    def routes(self) -> list[Route]:
        return [Route(method="GET", path="/ping/echo", handler=_handle_echo)]


plugin = PingPlugin()
