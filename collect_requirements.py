"""
collect_requirements.py — يجمع requirements من كل plugins ويكتب requirements.txt.

يُشغَّل مرة واحدة أثناء بناء Docker image (قبل pip install).
لا يستورد plugins ولا FastAPI — يقرأ الكود كـ AST نصي فقط.
هذا يعني أنه يعمل حتى قبل تثبيت أي حزمة.

ما يبحث عنه في كل ملف plugin:
  requirements = [...]
  pip_extra    = [...]
كحقول مباشرة في أول class-level assignment (قبل أي دالة).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT        = Path(__file__).parent
PLUGINS_DIR = ROOT / "plugins"
BASE_REQ    = ROOT / "requirements.base.txt"
OUT_REQ     = ROOT / "requirements.txt"


def _extract_list(node: ast.expr) -> list[str]:
    """استخرج قيم نصية من ast.List."""
    if not isinstance(node, ast.List):
        return []
    result = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            result.append(elt.value.strip())
    return result


def _parse_plugin_file(path: Path) -> tuple[list[str], list[str]]:
    """
    يقرأ ملف plugin كـ AST ويستخرج:
      - requirements: قائمة حزم pip
      - pip_extra:    سطور --extra-index-url وغيرها

    يبحث عن class assignments مباشرة:
      class MyPlugin(Plugin):
          requirements = ["requests", ...]
          pip_extra    = ["--extra-index-url ..."]
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        print(f"  ⚠️  [{path.stem}] خطأ في الصياغة: {exc}")
        return [], []

    reqs:  list[str] = []
    extra: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, ast.Assign):
                continue
            for target in item.targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id == "requirements":
                    reqs  = _extract_list(item.value)
                elif target.id == "pip_extra":
                    extra = _extract_list(item.value)

    return extra, reqs


def _pkg_name(line: str) -> str:
    """اسم الحزمة بدون version specifier — للكشف عن التكرار."""
    line = line.strip()
    for sep in (">=", "<=", "==", "!=", ">", "<", "[", "@", " "):
        line = line.split(sep)[0]
    return line.strip().lower().replace("-", "_").replace(".", "_")


def collect() -> tuple[list[str], list[str]]:
    all_extra: list[str] = []
    all_reqs:  list[str] = []
    seen_pkgs: set[str]  = set()

    for path in sorted(PLUGINS_DIR.glob("*.py")):
        if path.stem.startswith("_"):
            continue

        extra, reqs = _parse_plugin_file(path)
        if not extra and not reqs:
            continue

        name = path.stem

        for line in extra:
            if line and line not in all_extra:
                all_extra.append(line)
                print(f"  [{name}] pip_extra: {line}")

        for line in reqs:
            if not line:
                continue
            pkg = _pkg_name(line)
            if pkg in seen_pkgs:
                print(f"  [{name}] مكرر (متجاوَز): {line}")
                continue
            seen_pkgs.add(pkg)
            all_reqs.append(line)
            print(f"  [{name}] + {line}")

    return all_extra, all_reqs


def main() -> None:
    print("🔍 collect_requirements.py — جمع متطلبات plugins...\n")

    base_lines: list[str] = []
    if BASE_REQ.exists():
        base_lines = BASE_REQ.read_text().splitlines()
        count = len([l for l in base_lines if l.strip() and not l.startswith("#")])
        print(f"📄 requirements.base.txt: {count} حزمة\n")
    else:
        print(f"⚠️  {BASE_REQ} غير موجود\n")

    extra_lines, req_lines = collect()

    sections: list[str] = []

    if base_lines:
        sections.append("# ─── متطلبات البنية التحتية (ثابتة) ───────────────────────────")
        sections.extend(base_lines)

    if extra_lines:
        sections.append("")
        sections.append("# ─── pip extras من plugins ─────────────────────────────────────")
        sections.extend(extra_lines)

    if req_lines:
        sections.append("")
        sections.append("# ─── حزم plugins (مجموعة تلقائياً) ────────────────────────────")
        sections.extend(req_lines)

    OUT_REQ.write_text("\n".join(sections) + "\n")

    total = len(req_lines)
    print(f"\n✅ كُتب {OUT_REQ.name} — {total} حزمة من plugins")


if __name__ == "__main__":
    main()
