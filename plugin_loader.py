"""
plugin_loader.py
- يكتشف كل ملفات plugins/ تلقائياً
- يثبت متطلبات كل plugin من plugins/requirements/<name>.txt
- يسجل routes كل plugin على FastAPI app
- يحدّث Dockerfile و requirements.txt الجذر تلقائياً
- يقبل register(app) أو setup(app) أو كليهما
- فشل تثبيت متطلبة واحدة لا يوقف باقي الـ plugins
"""

import os
import sys
import glob
import subprocess
import importlib
import importlib.util
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("plugin_loader")

PLUGINS_DIR     = os.path.join(os.path.dirname(__file__), "plugins")
REQ_DIR         = os.path.join(PLUGINS_DIR, "requirements")
ROOT_REQ_FILE   = os.path.join(os.path.dirname(__file__), "requirements.txt")
DOCKERFILE_PATH = os.path.join(os.path.dirname(__file__), "Dockerfile")

# ═══════════════════════════════════════════════════
# حماية بسيطة بتوكن سري مشترك (X-Internal-Token)
# - يُقرأ من متغير البيئة INTERNAL_TOKEN
# - إن لم يُضبط، لا حماية (يبقى السلوك القديم — مع تحذير)
# - "/" و "/health" مستثناة عمداً (فحوصات حالة عامة لا تكشف شيئاً حساساً)
# ═══════════════════════════════════════════════════

INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "").strip()
PUBLIC_PATHS = {"/", "/health"}


def _register_auth_middleware(app):
    if not INTERNAL_TOKEN:
        logger.warning(
            "⚠️ INTERNAL_TOKEN غير مضبوط — كل الـ endpoints مفتوحة بدون حماية! "
            "أضف INTERNAL_TOKEN في إعدادات الـ Space (Settings → Variables and secrets)."
        )
        return

    @app.middleware("http")
    async def _internal_token_guard(request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        supplied = request.headers.get("x-internal-token", "")
        if supplied != INTERNAL_TOKEN:
            logger.warning(
                f"🚫 طلب مرفوض (توكن غير صحيح/مفقود) — {request.method} {request.url.path} "
                f"من {request.client.host if request.client else 'unknown'}"
            )
            return JSONResponse(
                status_code=401,
                content={"status": "error", "message": "Unauthorized — missing or invalid X-Internal-Token"},
            )

        return await call_next(request)

    logger.info("🔒 تم تفعيل حماية X-Internal-Token على كل الـ endpoints (عدا / و /health)")

# سجل الـ plugins المحمَّلة — يُعرض في /
_registry: dict = {}


def get_registry() -> dict:
    return _registry


# ═══════════════════════════════════════════════════
# تثبيت المتطلبات — فشل واحد لا يوقف الباقي
# ═══════════════════════════════════════════════════

def _install_requirements(plugin_name: str) -> bool:
    """
    يثبت متطلبات plugin معين إن وُجد ملف requirements له.
    يُعيد True دائماً حتى لو فشل التثبيت — الـ plugin يُحمَّل على أي حال
    ويُسجَّل الخطأ فقط كتحذير.
    """
    req_file = os.path.join(REQ_DIR, f"{plugin_name}.txt")
    if not os.path.exists(req_file):
        return True  # لا متطلبات خاصة — OK

    logger.info(f"[{plugin_name}] تثبيت المتطلبات من {req_file}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "-r", req_file, "--quiet", "--break-system-packages"],
            capture_output=True, text=True, timeout=180
        )
        if result.returncode != 0:
            # سجّل تحذير فقط — لا توقف التحميل
            logger.warning(
                f"[{plugin_name}] ⚠️ بعض المتطلبات لم تُثبَّت:\n{result.stderr[:300]}"
            )
        else:
            logger.info(f"[{plugin_name}] ✅ تم تثبيت المتطلبات")
    except subprocess.TimeoutExpired:
        logger.warning(f"[{plugin_name}] ⏱ انتهت مهلة تثبيت المتطلبات — المتابعة")
    except Exception as e:
        logger.warning(f"[{plugin_name}] ⚠️ خطأ في تثبيت المتطلبات: {e} — المتابعة")

    return True  # دائماً True — نحمّل الـ plugin حتى لو فشل التثبيت


# ═══════════════════════════════════════════════════
# تحديث requirements.txt الجذر
# ═══════════════════════════════════════════════════

def _sync_root_requirements():
    """
    يجمع كل ملفات plugins/requirements/*.txt
    ويضيف أي مكتبة غير موجودة في requirements.txt الجذر.
    """
    if not os.path.exists(REQ_DIR):
        return

    existing = set()
    if os.path.exists(ROOT_REQ_FILE):
        with open(ROOT_REQ_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    pkg = line.split("==")[0].split(">=")[0].split("<=")[0].strip().lower()
                    existing.add(pkg)

    new_lines = []
    for req_file in sorted(glob.glob(os.path.join(REQ_DIR, "*.txt"))):
        plugin_name = os.path.basename(req_file)[:-4]
        with open(req_file) as f:
            pkgs = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        added = []
        for pkg in pkgs:
            pkg_name = pkg.split("==")[0].split(">=")[0].split("<=")[0].strip().lower()
            if pkg_name not in existing:
                new_lines.append(pkg)
                existing.add(pkg_name)
                added.append(pkg)
        if added:
            logger.info(f"[sync] إضافة لـ requirements.txt: {added} (من {plugin_name})")

    if new_lines:
        with open(ROOT_REQ_FILE, "a") as f:
            f.write("\n# ─── auto-added by plugin_loader ───────────────────\n")
            f.write("\n".join(new_lines) + "\n")
        logger.info(f"[sync] ✅ تم تحديث requirements.txt بـ {len(new_lines)} مكتبة جديدة")


# ═══════════════════════════════════════════════════
# تحديث Dockerfile
# ═══════════════════════════════════════════════════

def _sync_dockerfile():
    """
    يفحص كل plugin عن apt packages مطلوبة (DOCKERFILE_DEPS قائمة في الـ plugin)
    ويضيفها لـ Dockerfile إن لم تكن موجودة.

    كل plugin يعلن متطلباته هكذا:
        DOCKERFILE_DEPS = ["ffmpeg", "libsndfile1"]
    """
    if not os.path.exists(DOCKERFILE_PATH):
        return

    with open(DOCKERFILE_PATH) as f:
        dockerfile = f.read()

    all_apt = set()
    for filepath in glob.glob(os.path.join(PLUGINS_DIR, "*.py")):
        spec = importlib.util.spec_from_file_location("_tmp", filepath)
        mod  = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            deps = getattr(mod, "DOCKERFILE_DEPS", [])
            all_apt.update(deps)
        except Exception:
            pass

    if not all_apt:
        return

    missing = [d for d in sorted(all_apt) if d not in dockerfile]
    if not missing:
        return

    insert_after = "apt-get install -y \\\n"
    if insert_after not in dockerfile:
        logger.warning("[dockerfile] لم يُعثر على كتلة apt-get install — تخطي")
        return

    additions = "".join(f"    {pkg} \\\n" for pkg in missing)
    dockerfile = dockerfile.replace(insert_after, insert_after + additions, 1)

    with open(DOCKERFILE_PATH, "w") as f:
        f.write(dockerfile)

    logger.info(f"[dockerfile] ✅ أضيف لـ Dockerfile: {missing}")


# ═══════════════════════════════════════════════════
# تحميل الـ plugins
# ═══════════════════════════════════════════════════

def load_all_plugins(app):
    """
    نقطة الدخول الرئيسية — يُستدعى مرة واحدة من main.py
    """
    os.makedirs(PLUGINS_DIR, exist_ok=True)
    os.makedirs(REQ_DIR,     exist_ok=True)

    # سجّل حماية التوكن السري قبل أي شيء آخر — لازم middleware يُضاف
    # قبل أن يبدأ التطبيق بالتعامل مع الطلبات (FastAPI يبني سلسلة الـ
    # middleware عند الإقلاع)
    _register_auth_middleware(app)

    _sync_root_requirements()
    _sync_dockerfile()

    plugin_files = sorted(glob.glob(os.path.join(PLUGINS_DIR, "*.py")))
    # تجاهل __init__.py و _base.py
    plugin_files = [f for f in plugin_files
                    if not os.path.basename(f).startswith("_")]

    loaded = 0
    failed = 0
    for filepath in plugin_files:
        plugin_name = os.path.basename(filepath)[:-3]
        success = _load_one_plugin(app, plugin_name, filepath)
        if success:
            loaded += 1
        else:
            failed += 1

    logger.info(f"[plugin_loader] ✅ تم تحميل {loaded} plugin(s)" +
                (f" | ❌ فشل {failed}" if failed else ""))


def _load_one_plugin(app, name: str, filepath: str) -> bool:
    """
    يحمل plugin واحد — يثبت متطلباته ويسجل routes.
    يقبل: register(app) أو setup(app) — أيهما موجود.
    يُعيد True عند النجاح، False عند الفشل الكامل.
    """
    try:
        # 1. ثبّت المتطلبات (لا يوقف التحميل عند الفشل)
        _install_requirements(name)

        # 2. حمّل الـ module
        spec   = importlib.util.spec_from_file_location(f"plugins.{name}", filepath)
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"plugins.{name}"] = module
        spec.loader.exec_module(module)

        # 3. استدعي register(app) أو setup(app) — أيهما موجود أولاً
        entry_fn = None
        if hasattr(module, "register"):
            entry_fn = module.register
        elif hasattr(module, "setup"):
            entry_fn = module.setup
            logger.info(f"[{name}] ℹ️ يستخدم setup() بدلاً من register()")
        else:
            logger.warning(f"[{name}] ⚠️ لا توجد دالة register() أو setup() — تخطي")
            _registry[name] = {"status": "skipped", "reason": "no register/setup function"}
            return False

        routes_before = len(app.routes)
        entry_fn(app)
        new_routes = len(app.routes) - routes_before

        # 4. سجل في الـ registry
        description = getattr(module, "DESCRIPTION", "")
        _registry[name] = {
            "status":       "loaded",
            "routes_added": new_routes,
            "description":  description,
        }
        logger.info(f"[{name}] ✅ محمَّل — {new_routes} route(s) مضافة")
        return True

    except ImportError as e:
        # مكتبة مفقودة — سجّل تحذير واضح
        logger.error(f"[{name}] ❌ مكتبة مفقودة: {e}")
        _registry[name] = {"status": "error", "reason": f"ImportError: {e}"}
        return False

    except Exception as e:
        logger.exception(f"[{name}] ❌ خطأ أثناء التحميل: {e}")
        _registry[name] = {"status": "error", "reason": str(e)}
        return False
