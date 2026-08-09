"""
plugins/dama.py — Plugin لعبة الداما (Checkers).

Endpoints:
  POST /dama/new_game — يبدأ لعبة جديدة ويعيد الرقعة الأولية
  POST /dama/move     — يطبّق حركة ويعيد الرقعة المحدّثة + PNG base64

نظام الإحداثيات:
  الأعمدة: A–H  (0–7 داخلياً)
  الصفوف:  1–8  (0–7 داخلياً، 0 = أسفل = جهة الأحمر)
  المربعات الداكنة فقط: (col + row) % 2 == 1
  الأحمر  (R)  يبدأ من الأسفل (صفوف 0-2) ويتحرك للأعلى (+row)
  الأزرق  (B)  يبدأ من الأعلى (صفوف 5-7) ويتحرك للأسفل (−row)

الرقعة كـ list مسطّح 64 عنصر:
  board[row * 8 + col] = None | "R" | "B" | "KR" | "KB"

القواعد المطبّقة:
  • القفز إجباري دائماً
  • القفز المتعدد إجباري بنفس القطعة ما لم تتحوّل لملك
  • التحوّل لملك ينهي القفز المتعدد (قواعد الداما الأمريكية)

البنية التحتية (Dockerfile):
  apt-get install libcairo2 libpango-1.0-0 libpangocairo-1.0-0
  pip: cairosvg
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Optional

import cairosvg
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from internal.plugin import Plugin, Route

log = logging.getLogger(__name__)

# ─── إعدادات ─────────────────────────────────────────────────────────────────

BOARD_SIZE = int(os.environ.get("BOARD_SIZE", "480"))

# ─── ثوابت القطع ─────────────────────────────────────────────────────────────

R  = "R"    # قطعة حمراء عادية
B  = "B"    # قطعة زرقاء عادية
KR = "KR"   # ملك أحمر
KB = "KB"   # ملك أزرق

_RED_PIECES  = frozenset({R,  KR})
_BLUE_PIECES = frozenset({B,  KB})
_ALL_PIECES  = _RED_PIECES | _BLUE_PIECES

_DIRS_R   = [( 1, 1), (-1, 1)]           # أحمر عادي  → أعلى
_DIRS_B   = [( 1,-1), (-1,-1)]           # أزرق عادي  → أسفل
_DIRS_ALL = [( 1, 1), (-1, 1), ( 1,-1), (-1,-1)]  # ملوك

# ─── دوال مساعدة للرقعة ──────────────────────────────────────────────────────

def _idx(col: int, row: int) -> int:
    return row * 8 + col

def _pos(i: int) -> tuple[int, int]:
    return i % 8, i // 8

def _is_dark(col: int, row: int) -> bool:
    return (col + row) % 2 == 1

def _is_red(p: Optional[str])  -> bool: return p in _RED_PIECES
def _is_blue(p: Optional[str]) -> bool: return p in _BLUE_PIECES
def _is_king(p: Optional[str]) -> bool: return p in (KR, KB)

def _opp_set(p: str) -> frozenset:
    return _BLUE_PIECES if _is_red(p) else _RED_PIECES

def _move_dirs(p: str) -> list[tuple[int, int]]:
    if p == R:  return _DIRS_R
    if p == B:  return _DIRS_B
    return _DIRS_ALL

def _coord_str(col: int, row: int) -> str:
    return f"{chr(ord('A') + col)}{row + 1}"

def _parse_coord(s: str) -> Optional[tuple[int, int]]:
    """تحليل "A3" → (col=0, row=2)، None إذا كانت غير صالحة."""
    s = s.strip().upper()
    if len(s) < 2:
        return None
    col = ord(s[0]) - ord("A")
    try:
        row = int(s[1:]) - 1
    except ValueError:
        return None
    return (col, row) if (0 <= col < 8 and 0 <= row < 8) else None


# ─── منطق الرقعة ─────────────────────────────────────────────────────────────

def initial_board() -> list:
    """يُنشئ رقعة البداية — 12 قطعة لكل لون."""
    board: list[Optional[str]] = [None] * 64
    for row in range(8):
        for col in range(8):
            if _is_dark(col, row):
                if row <= 2:
                    board[_idx(col, row)] = R
                elif row >= 5:
                    board[_idx(col, row)] = B
    return board


def _get_jumps(board: list, col: int, row: int) -> list[tuple[int, int, int, int]]:
    """
    يُعيد قائمة القفزات المتاحة من (col, row).
    كل إدخال: (to_col, to_row, cap_col, cap_row)
    """
    piece = board[_idx(col, row)]
    if piece is None:
        return []
    opp = _opp_set(piece)
    jumps = []
    for dc, dr in _move_dirs(piece):
        mc, mr = col + dc, row + dr
        tc, tr = col + 2*dc, row + 2*dr
        if not (0 <= mc < 8 and 0 <= mr < 8): continue
        if not (0 <= tc < 8 and 0 <= tr < 8): continue
        if board[_idx(mc, mr)] not in opp:    continue
        if board[_idx(tc, tr)] is not None:    continue
        jumps.append((tc, tr, mc, mr))
    return jumps


def _get_simple_moves(board: list, col: int, row: int) -> list[tuple[int, int]]:
    """يُعيد قائمة الحركات العادية (بدون قفز) من (col, row)."""
    piece = board[_idx(col, row)]
    if piece is None:
        return []
    moves = []
    for dc, dr in _move_dirs(piece):
        tc, tr = col + dc, row + dr
        if not (0 <= tc < 8 and 0 <= tr < 8): continue
        if board[_idx(tc, tr)] is None:
            moves.append((tc, tr))
    return moves


def _has_any_jump(board: list, turn: str) -> bool:
    """هل يملك اللاعب الحالي أي قفزة؟"""
    is_mine = _is_red if turn == R else _is_blue
    for i, p in enumerate(board):
        if p and is_mine(p):
            col, row = _pos(i)
            if _get_jumps(board, col, row):
                return True
    return False


def validate_move(
    board: list,
    fc: int, fr: int,
    tc: int, tr: int,
    turn: str,
    mjp: Optional[list],
) -> Optional[str]:
    """
    يتحقق من صحة الحركة.
    mjp = [col, row] للقطعة المُلزَمة بالقفز المتعدد، أو None.
    يُعيد رسالة خطأ أو None إذا كانت الحركة صالحة.
    """
    piece = board[_idx(fc, fr)]

    if piece is None:
        return f"❌ لا توجد قطعة في {_coord_str(fc, fr)}"
    if turn == R and not _is_red(piece):
        return "❌ هذه ليست قطعة الأحمر"
    if turn == B and not _is_blue(piece):
        return "❌ هذه ليست قطعة الأزرق"
    if not (0 <= tc < 8 and 0 <= tr < 8):
        return "❌ الإحداثيات خارج الرقعة"
    if not _is_dark(tc, tr):
        return "❌ يجب التحرك على المربعات الداكنة فقط"
    if board[_idx(tc, tr)] is not None:
        return f"❌ المربع {_coord_str(tc, tr)} مشغول"

    dc = tc - fc
    dr = tr - fr

    if abs(dc) == 1 and abs(dr) == 1:
        # حركة عادية (بدون قفز)
        if mjp is not None:
            return f"❌ يجب الاستمرار في القفز بالقطعة في {_coord_str(mjp[0], mjp[1])}"
        if _has_any_jump(board, turn):
            return "❌ القفز إجباري — يجب قفز فوق قطعة الخصم"
        if (dc, dr) not in _move_dirs(piece):
            return "❌ اتجاه الحركة غير مسموح لهذه القطعة"
        return None

    if abs(dc) == 2 and abs(dr) == 2:
        # قفزة
        if (dc // 2, dr // 2) not in _move_dirs(piece):
            return "❌ اتجاه القفز غير مسموح لهذه القطعة"
        if mjp is not None and [fc, fr] != list(mjp):
            return f"❌ يجب الاستمرار بالقطعة في {_coord_str(mjp[0], mjp[1])}"
        mc, mr = fc + dc // 2, fr + dr // 2
        mid = board[_idx(mc, mr)]
        if mid not in _opp_set(piece):
            return f"❌ لا توجد قطعة خصم في {_coord_str(mc, mr)} للقفز عليها"
        return None

    return "❌ حركة غير صالحة — يجب أن تكون الحركة قطرية بمربع أو مربعين"


def apply_move(
    board: list,
    fc: int, fr: int,
    tc: int, tr: int,
) -> tuple[list, bool]:
    """
    يُطبّق الحركة ويُعيد (new_board, became_king).
    لا يتحقق من الصحة — استدعِ validate_move أولاً.
    """
    board = list(board)
    piece = board[_idx(fc, fr)]
    board[_idx(fc, fr)] = None

    is_jump = abs(tc - fc) == 2
    if is_jump:
        mc, mr = (fc + tc) // 2, (fr + tr) // 2
        board[_idx(mc, mr)] = None

    # التحوّل لملك
    became_king = False
    if piece == R and tr == 7:
        piece = KR
        became_king = True
    elif piece == B and tr == 0:
        piece = KB
        became_king = True

    board[_idx(tc, tr)] = piece
    return board, became_king


def check_game_over(board: list, next_turn: str) -> tuple[bool, Optional[str]]:
    """
    يتحقق من انتهاء اللعبة.
    يُعيد (game_over, winner) حيث winner ∈ {"R","B",None(=تعادل)}.
    """
    r_count = sum(1 for p in board if p in _RED_PIECES)
    b_count = sum(1 for p in board if p in _BLUE_PIECES)

    if r_count == 0: return True, B
    if b_count == 0: return True, R

    is_mine = _is_red if next_turn == R else _is_blue
    for i, p in enumerate(board):
        if p and is_mine(p):
            col, row = _pos(i)
            if _get_jumps(board, col, row) or _get_simple_moves(board, col, row):
                return False, None

    # اللاعب التالي بلا حركات — الخصم يفوز
    return True, (B if next_turn == R else R)


# ─── رسم الرقعة (SVG → PNG) ──────────────────────────────────────────────────

_SQ = BOARD_SIZE // 8   # حجم المربع الواحد بالبكسل

_C = {
    "light":      "#FFFFFF",
    "dark":       "#1A1A1A",
    "last":       "#004400",
    "grid":       "#00CC44",
    "border_out": "#000000",
    "border_in":  "#00CC44",
    "sidebar_bg": "#111111",
    "coord_fg":   "#00FF66",
    "coord_font": "Courier New, monospace",
    "r_fill":     "#CC2200",
    "r_stroke":   "#FF4422",
    "r_shine":    "#FF7755",
    "b_fill":     "#1155CC",
    "b_stroke":   "#4488FF",
    "b_shine":    "#66AAFF",
    "shadow":     "rgba(0,0,0,0.45)",
}


def _board_svg(
    board: list,
    last_from: Optional[tuple[int, int]] = None,
    last_to:   Optional[tuple[int, int]] = None,
) -> str:
    """يولّد SVG للرقعة بتصميم أسود/أبيض مع خطوط خضراء وشريط جانبي."""
    sq       = _SQ
    S        = BOARD_SIZE
    SIDEBAR  = sq                      # عرض الشريط الجانبي = عرض مربع
    FOOT     = sq // 2                 # ارتفاع شريط الأسفل
    BORDER   = 4                       # سمك الحدود الخضراء الداخلية
    OUT_B    = 6                       # سمك الحدود الخارجية السوداء
    GRID_W   = 1                       # سمك خطوط الشبكة

    # إجمالي أبعاد الصورة
    W = S + SIDEBAR + OUT_B * 2        # عرض كامل = رقعة + شريط + حدود
    H = S + FOOT    + OUT_B * 2        # ارتفاع كامل = رقعة + تذييل + حدود

    # إزاحة الرقعة داخل الصورة
    OX = OUT_B + SIDEBAR               # X بداية الرقعة (بعد الحدود والشريط)
    OY = OUT_B                         # Y بداية الرقعة

    last_sq  = {last_from, last_to} - {None}
    fnt      = max(sq // 4, 12)
    fnt_sm   = max(sq // 5, 10)

    lines: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">',

        # ── خلفية كاملة سوداء ──────────────────────────────────────────────
        f'<rect width="{W}" height="{H}" fill="{_C["border_out"]}"/>',

        # ── خلفية الشريط الجانبي ───────────────────────────────────────────
        f'<rect x="{OUT_B}" y="{OUT_B}" width="{SIDEBAR}" height="{S}"'
        f' fill="{_C["sidebar_bg"]}"/>',

        # ── حدود داخلية خضراء حول الرقعة ──────────────────────────────────
        f'<rect x="{OX - BORDER}" y="{OY - BORDER}"'
        f' width="{S + BORDER*2}" height="{S + BORDER*2}"'
        f' fill="none" stroke="{_C["border_in"]}" stroke-width="{BORDER}"/>',
    ]

    # ── رسم مربعات الرقعة ────────────────────────────────────────────────────
    for row in range(7, -1, -1):
        for col in range(8):
            x = OX + col * sq
            y = OY + (7 - row) * sq

            if (col, row) in last_sq:
                fill = _C["last"]
            elif _is_dark(col, row):
                fill = _C["dark"]
            else:
                fill = _C["light"]

            lines.append(
                f'<rect x="{x}" y="{y}" width="{sq}" height="{sq}" fill="{fill}"/>'
            )

    # ── خطوط الشبكة الخضراء (فوق المربعات) ──────────────────────────────────
    for i in range(9):   # 9 خطوط عمودية
        lx = OX + i * sq
        lines.append(
            f'<line x1="{lx}" y1="{OY}" x2="{lx}" y2="{OY + S}"'
            f' stroke="{_C["grid"]}" stroke-width="{GRID_W}"/>'
        )
    for i in range(9):   # 9 خطوط أفقية
        ly = OY + i * sq
        lines.append(
            f'<line x1="{OX}" y1="{ly}" x2="{OX + S}" y2="{ly}"'
            f' stroke="{_C["grid"]}" stroke-width="{GRID_W}"/>'
        )

    # ── رسم القطع ─────────────────────────────────────────────────────────────
    for row in range(7, -1, -1):
        for col in range(8):
            piece = board[_idx(col, row)]
            if piece is None:
                continue

            x  = OX + col * sq
            y  = OY + (7 - row) * sq
            cx = x + sq // 2
            cy = y + sq // 2
            r  = sq * 0.38

            # ظل
            lines.append(
                f'<circle cx="{cx+2}" cy="{cy+3}" r="{r:.1f}" fill="{_C["shadow"]}"/>'
            )

            if _is_red(piece):
                fc, sc, sh = _C["r_fill"], _C["r_stroke"], _C["r_shine"]
            else:
                fc, sc, sh = _C["b_fill"], _C["b_stroke"], _C["b_shine"]

            lines.append(
                f'<circle cx="{cx}" cy="{cy}" r="{r:.1f}"'
                f' fill="{fc}" stroke="{sc}" stroke-width="2.5"/>'
            )
            lines.append(
                f'<circle cx="{cx - r*0.25:.1f}" cy="{cy - r*0.25:.1f}"'
                f' r="{r * 0.3:.1f}" fill="{sh}" opacity="0.5"/>'
            )

            if _is_king(piece):
                fs = int(r * 1.1)
                lines.append(
                    f'<text x="{cx}" y="{cy + r*0.42:.1f}"'
                    f' text-anchor="middle" dominant-baseline="middle"'
                    f' font-size="{fs}" fill="gold" font-family="serif">♛</text>'
                )

    # ── شريط جانبي: أرقام الصفوف (1–8) ──────────────────────────────────────
    for row in range(8):
        lbl = str(row + 1)
        cy  = OY + (7 - row) * sq + sq // 2
        lines.append(
            f'<text x="{OUT_B + SIDEBAR // 2}" y="{cy}"'
            f' text-anchor="middle" dominant-baseline="central"'
            f' font-size="{fnt}" font-weight="bold"'
            f' fill="{_C["coord_fg"]}" font-family="{_C["coord_font"]}">{lbl}</text>'
        )

    # ── شريط أسفل: حروف الأعمدة (A–H) ───────────────────────────────────────
    for col in range(8):
        lbl = chr(ord("A") + col)
        cx  = OX + col * sq + sq // 2
        cy  = OY + S + FOOT // 2
        lines.append(
            f'<text x="{cx}" y="{cy}"'
            f' text-anchor="middle" dominant-baseline="central"'
            f' font-size="{fnt}" font-weight="bold"'
            f' fill="{_C["coord_fg"]}" font-family="{_C["coord_font"]}">{lbl}</text>'
        )

    # ── حد خضر بين الشريط والرقعة ────────────────────────────────────────────
    lines.append(
        f'<line x1="{OX}" y1="{OY}" x2="{OX}" y2="{OY + S}"'
        f' stroke="{_C["border_in"]}" stroke-width="{BORDER}"/>'
    )
    # حد خضر بين الرقعة والتذييل
    lines.append(
        f'<line x1="{OX}" y1="{OY + S}" x2="{OX + S}" y2="{OY + S}"'
        f' stroke="{_C["border_in"]}" stroke-width="{BORDER}"/>'
    )

    lines.append("</svg>")
    return "\n".join(lines)


def _board_png_b64(
    board:     list,
    last_from: Optional[tuple[int, int]] = None,
    last_to:   Optional[tuple[int, int]] = None,
) -> str:
    """يُحوّل الرقعة إلى PNG base64 جاهزة للإرسال."""
    svg = _board_svg(board, last_from, last_to)
    # الأبعاد الفعلية = رقعة + شريط جانبي + تذييل + حدود
    _sidebar = _SQ
    _foot    = _SQ // 2
    _outb    = 6
    out_w    = BOARD_SIZE + _sidebar + _outb * 2
    out_h    = BOARD_SIZE + _foot    + _outb * 2
    png = cairosvg.svg2png(
        bytestring    = svg.encode("utf-8"),
        output_width  = out_w,
        output_height = out_h,
    )
    return base64.b64encode(png).decode("utf-8")


# ─── نماذج Pydantic ──────────────────────────────────────────────────────────

class NewGameRequest(BaseModel):
    pass   # لا معاملات — الرقعة الأولية ثابتة


class MoveRequest(BaseModel):
    board:          list               # رقعة 64 عنصر
    from_sq:        str = Field(alias="from")   # مثال "C3"
    to_sq:          str = Field(alias="to")     # مثال "D4"
    turn:           str                # "R" | "B"
    multi_jump_pos: Optional[list] = None       # [col, row] إذا كان قفزاً متعدداً

    model_config = {"populate_by_name": True}


# ─── Handlers ────────────────────────────────────────────────────────────────

async def _handle_new_game(_: NewGameRequest) -> JSONResponse:
    """POST /dama/new_game — يُهيئ لعبة جديدة."""
    board = initial_board()
    return JSONResponse({
        "board":       board,
        "next_turn":   R,    # الأحمر يبدأ دائماً
        "image_base64": _board_png_b64(board),
    })


async def _handle_move(req: MoveRequest) -> JSONResponse:
    """POST /dama/move — يُطبّق حركة ويُعيد الحالة الجديدة."""

    # ── تحليل الإحداثيات ─────────────────────────────────────────────────────
    from_pos = _parse_coord(req.from_sq)
    to_pos   = _parse_coord(req.to_sq)

    if from_pos is None:
        return JSONResponse({
            "success": False,
            "error":   f"❌ إحداثيات البداية غير صالحة: {req.from_sq}",
            "board":   req.board, "next_turn": req.turn,
            "image_base64": None, "game_over": False, "winner": None,
            "multi_jump_pos": req.multi_jump_pos,
        })
    if to_pos is None:
        return JSONResponse({
            "success": False,
            "error":   f"❌ إحداثيات الهدف غير صالحة: {req.to_sq}",
            "board":   req.board, "next_turn": req.turn,
            "image_base64": None, "game_over": False, "winner": None,
            "multi_jump_pos": req.multi_jump_pos,
        })

    fc, fr = from_pos
    tc, tr = to_pos
    board  = list(req.board)

    # ── التحقق من صحة الحركة ─────────────────────────────────────────────────
    err = validate_move(board, fc, fr, tc, tr, req.turn, req.multi_jump_pos)
    if err:
        return JSONResponse({
            "success": False, "error": err,
            "board": board, "next_turn": req.turn,
            "image_base64": None, "game_over": False, "winner": None,
            "multi_jump_pos": req.multi_jump_pos,
        })

    # ── تطبيق الحركة ─────────────────────────────────────────────────────────
    new_board, became_king = apply_move(board, fc, fr, tc, tr)

    is_jump      = abs(tc - fc) == 2
    next_turn    = req.turn
    multi_jump_pos: Optional[list] = None

    if is_jump and not became_king:
        # هل يمكن الاستمرار بالقفز من الموضع الجديد؟
        more = _get_jumps(new_board, tc, tr)
        if more:
            multi_jump_pos = [tc, tr]
            # next_turn يبقى كما هو (نفس اللاعب يكمل)
        else:
            next_turn = B if req.turn == R else R
    else:
        # حركة عادية أو تحوّل لملك — ينتقل الدور
        next_turn = B if req.turn == R else R

    # ── فحص نهاية اللعبة ─────────────────────────────────────────────────────
    game_over, winner = check_game_over(new_board, next_turn)
    if game_over:
        multi_jump_pos = None   # لا معنى للقفز المتعدد بعد انتهاء اللعبة

    # ── رسم الرقعة ───────────────────────────────────────────────────────────
    img_b64 = _board_png_b64(new_board, (fc, fr), (tc, tr))

    return JSONResponse({
        "success":       True,
        "error":         None,
        "board":         new_board,
        "next_turn":     next_turn,
        "image_base64":  img_b64,
        "game_over":     game_over,
        "winner":        winner,          # "R" | "B" | null
        "multi_jump_pos": multi_jump_pos, # [col,row] | null
    })


# ─── Plugin ──────────────────────────────────────────────────────────────────

class DamaPlugin(Plugin):
    """Plugin لعبة الداما — POST /dama/new_game و /dama/move."""

    description  = "محرك داما (Checkers) رسومي — صور PNG لشات فيسبوك"
    requirements = ["cairosvg>=2.7.1"]

    def name(self) -> str:
        return "dama"

    def routes(self) -> list[Route]:
        return [
            Route(method="POST", path="/dama/new_game", handler=_handle_new_game),
            Route(method="POST", path="/dama/move",     handler=_handle_move),
        ]

    def startup(self) -> None:
        # اختبار سريع للتأكد من أن الرقعة تُولَّد بشكل صحيح
        b = initial_board()
        assert b[_idx(1, 0)] == R,  "Red piece expected at B1"
        assert b[_idx(0, 7)] == B,  "Blue piece expected at A8"
        assert b[_idx(0, 3)] is None, "Row 4 should be empty"
        log.info("✅ [dama] جاهز — %d قطعة حمراء، %d زرقاء",
                 sum(1 for p in b if _is_red(p)),
                 sum(1 for p in b if _is_blue(p)))

    def status(self) -> dict:
        b = initial_board()
        return {
            "status":       "loaded",
            "board_size":   BOARD_SIZE,
            "initial_red":  sum(1 for p in b if _is_red(p)),
            "initial_blue": sum(1 for p in b if _is_blue(p)),
        }


plugin = DamaPlugin()
