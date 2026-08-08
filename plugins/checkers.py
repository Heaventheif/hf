# plugins/checkers.py
# Checkers (Dama) game engine with SVG board generation.
# Follows the SunkenBot plugin contract — add this file to plugins/ only.
from __future__ import annotations

import copy
import logging
from typing import List, Optional, Tuple

from fastapi import HTTPException
from internal.plugin import Plugin, Route
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

EMPTY_DARK  = "⬛"   # Playable dark square (empty)
EMPTY_LIGHT = "⬜"   # Non-playable light square
RED         = "R"    # Regular red piece
BLUE        = "B"    # Regular blue piece
KING_RED    = "KR"   # Red king
KING_BLUE   = "KB"   # Blue king
BOARD_SIZE  = 8

# ─── Type Aliases ─────────────────────────────────────────────────────────────

Board = List[List[str]]
# A move is expressed as (from_row, from_col, to_row, to_col)
Move  = Tuple[int, int, int, int]

# ─── Request / Response Models ────────────────────────────────────────────────

class NewGameResponse(BaseModel):
    board:      List[List[str]]
    next_turn:  str
    svg:        str
    game_over:  bool
    winner:     Optional[str] = None


class MoveRequest(BaseModel):
    """
    Accepts the JSON payload for a move.
    'from' is a Python keyword so we alias it to from_sq internally.
    """
    board:   List[List[str]]
    from_sq: str = Field(alias="from")   # e.g. "A3"
    to_sq:   str = Field(alias="to")     # e.g. "B4"
    turn:    str                         # "R" or "B"

    model_config = {"populate_by_name": True}


class MoveResponse(BaseModel):
    success:    bool
    board:      Optional[List[List[str]]] = None
    next_turn:  Optional[str]             = None
    svg:        Optional[str]             = None
    game_over:  bool                      = False
    winner:     Optional[str]             = None
    error:      Optional[str]             = None


# ─── Board Initialisation ─────────────────────────────────────────────────────

def _init_board() -> Board:
    """
    Build a fresh 8×8 board.
    Dark squares are where (row + col) % 2 == 1.
    Blue occupies rows 0-2 (top), Red occupies rows 5-7 (bottom).
    """
    board: Board = []
    for row in range(BOARD_SIZE):
        row_cells: List[str] = []
        for col in range(BOARD_SIZE):
            if (row + col) % 2 == 1:           # Dark (playable) square
                if row < 3:
                    row_cells.append(BLUE)
                elif row > 4:
                    row_cells.append(RED)
                else:
                    row_cells.append(EMPTY_DARK)
            else:
                row_cells.append(EMPTY_LIGHT)  # Light (non-playable) square
        board.append(row_cells)
    return board


# ─── Coordinate Helpers ───────────────────────────────────────────────────────

def _parse_coord(coord: str) -> Tuple[int, int]:
    """
    Convert alphanumeric notation to (row_index, col_index).

    Mapping rules:
      Column  A → 0,  B → 1, … H → 7
      Row     8 → index 0  (top),  1 → index 7  (bottom)
      Formula: row_index = 8 - row_number
    """
    coord = coord.strip().upper()
    if len(coord) < 2:
        raise ValueError(f"Coordinate too short: '{coord}'")

    col_char = coord[0]
    row_str  = coord[1:]

    if col_char not in "ABCDEFGH":
        raise ValueError(f"Column '{col_char}' is out of range A-H.")

    try:
        row_num = int(row_str)
    except ValueError:
        raise ValueError(f"Row '{row_str}' is not a valid integer.")

    if not 1 <= row_num <= 8:
        raise ValueError(f"Row number {row_num} must be between 1 and 8.")

    col = ord(col_char) - ord("A")
    row = 8 - row_num
    return row, col


# ─── Piece Predicates ─────────────────────────────────────────────────────────

def _is_red(piece: str)  -> bool: return piece in (RED, KING_RED)
def _is_blue(piece: str) -> bool: return piece in (BLUE, KING_BLUE)
def _is_king(piece: str) -> bool: return piece in (KING_RED, KING_BLUE)


def _belongs_to(piece: str, player: str) -> bool:
    """Return True when the piece belongs to player ('R' or 'B')."""
    return _is_red(piece) if player == RED else _is_blue(piece)


def _is_opponent(piece: str, player: str) -> bool:
    """Return True when the piece belongs to the opponent of player."""
    return _is_blue(piece) if player == RED else _is_red(piece)


# ─── Movement Directions ──────────────────────────────────────────────────────

def _move_dirs(piece: str, player: str) -> List[Tuple[int, int]]:
    """
    Valid diagonal directions for a piece.
    Red moves *up* (decreasing row index).
    Blue moves *down* (increasing row index).
    Kings move in all four diagonal directions.
    """
    if _is_king(piece):
        return [(-1, -1), (-1, 1), (1, -1), (1, 1)]
    if player == RED:
        return [(-1, -1), (-1, 1)]   # Red advances upward
    return [(1, -1), (1, 1)]          # Blue advances downward


# ─── Move Generation ──────────────────────────────────────────────────────────

def _captures_for_piece(board: Board, row: int, col: int, player: str) -> List[Move]:
    """Return all legal capture moves for the piece at (row, col)."""
    piece    = board[row][col]
    captures: List[Move] = []
    for dr, dc in _move_dirs(piece, player):
        mid_r, mid_c = row + dr,      col + dc
        to_r,  to_c  = row + 2 * dr,  col + 2 * dc
        # Bounds check
        if not (0 <= mid_r < 8 and 0 <= mid_c < 8):
            continue
        if not (0 <= to_r  < 8 and 0 <= to_c  < 8):
            continue
        # Jump over an opponent into an empty dark square
        if _is_opponent(board[mid_r][mid_c], player) \
                and board[to_r][to_c] == EMPTY_DARK:
            captures.append((row, col, to_r, to_c))
    return captures


def _simple_moves_for_piece(board: Board, row: int, col: int, player: str) -> List[Move]:
    """Return all legal single-step moves for the piece at (row, col)."""
    piece = board[row][col]
    moves: List[Move] = []
    for dr, dc in _move_dirs(piece, player):
        to_r, to_c = row + dr, col + dc
        if 0 <= to_r < 8 and 0 <= to_c < 8 \
                and board[to_r][to_c] == EMPTY_DARK:
            moves.append((row, col, to_r, to_c))
    return moves


def _all_captures(board: Board, player: str) -> List[Move]:
    """Aggregate all capture moves available to a player across the whole board."""
    captures: List[Move] = []
    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            if _belongs_to(board[r][c], player):
                captures.extend(_captures_for_piece(board, r, c, player))
    return captures


def _all_legal_moves(board: Board, player: str) -> List[Move]:
    """
    Return every legal move for player, enforcing the mandatory-capture rule:
    if any capture is available, only captures are returned.
    """
    captures = _all_captures(board, player)
    if captures:
        return captures          # Mandatory jump — simple moves are forbidden

    moves: List[Move] = []
    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            if _belongs_to(board[r][c], player):
                moves.extend(_simple_moves_for_piece(board, r, c, player))
    return moves


# ─── Board Mutation ───────────────────────────────────────────────────────────

def _apply_move(board: Board, fr: int, fc: int, tr: int, tc: int) -> Tuple[Board, bool]:
    """
    Deep-copy the board, execute the move and return (new_board, was_capture).
    Removes the jumped piece when the distance is 2 (a capture jump).
    """
    nb = copy.deepcopy(board)
    piece = nb[fr][fc]

    nb[tr][tc] = piece
    nb[fr][fc] = EMPTY_DARK

    captured = abs(tr - fr) == 2
    if captured:
        mid_r = (fr + tr) // 2
        mid_c = (fc + tc) // 2
        nb[mid_r][mid_c] = EMPTY_DARK   # Remove the jumped piece

    _promote_kings(nb)
    return nb, captured


def _promote_kings(board: Board) -> None:
    """
    Promote pieces that have reached the opposite back rank.
    Red → row 0 (display row 8).
    Blue → row 7 (display row 1).
    Mutates the board in place.
    """
    for c in range(BOARD_SIZE):
        if board[0][c] == RED:
            board[0][c] = KING_RED
        if board[7][c] == BLUE:
            board[7][c] = KING_BLUE


# ─── Game-Over Detection ──────────────────────────────────────────────────────

def _check_game_over(board: Board, player: str) -> Tuple[bool, Optional[str]]:
    """
    Determine whether the game is over for the player whose turn is next.
    Returns (game_over, winner_or_None).
    A player loses if they have no pieces left or no legal moves.
    """
    has_pieces = any(
        _belongs_to(board[r][c], player)
        for r in range(BOARD_SIZE)
        for c in range(BOARD_SIZE)
    )
    if not has_pieces:
        return True, (BLUE if player == RED else RED)

    if not _all_legal_moves(board, player):
        return True, (BLUE if player == RED else RED)

    return False, None


# ─── SVG Generator ────────────────────────────────────────────────────────────

# Visual theme constants
_LIGHT_SQ  = "#f0d9b5"
_DARK_SQ   = "#b58863"
_RED_FILL  = "#d9534f"
_BLUE_FILL = "#0275d8"
_HIGHLIGHT = "#8ee4af"
_GOLD      = "#FFD700"
_STROKE    = "#222222"
_LABEL_CLR = "#dddddd"
_BG        = "#1e1e2e"

_TOTAL_PX  = 440   # Full SVG canvas size
_OFFSET    = 20    # Left/top margin reserved for axis labels
_BOARD_PX  = 400   # Inner board area
_CELL      = _BOARD_PX // BOARD_SIZE   # 50 px per cell


def _sq_star_path(cx: int, cy: int, outer: int, inner: int, points: int = 5) -> str:
    """
    Generate an SVG <path d="..."> for a star polygon centred at (cx, cy).
    Used to mark king pieces with a gold star.
    """
    import math
    path_parts = []
    for i in range(points * 2):
        r   = outer if i % 2 == 0 else inner
        ang = math.pi * i / points - math.pi / 2
        x   = cx + r * math.cos(ang)
        y   = cy + r * math.sin(ang)
        cmd = "M" if i == 0 else "L"
        path_parts.append(f"{cmd}{x:.2f},{y:.2f}")
    path_parts.append("Z")
    return " ".join(path_parts)


def _generate_svg(
    board:      Board,
    last_from:  Optional[Tuple[int, int]] = None,
    last_to:    Optional[Tuple[int, int]] = None,
) -> str:
    """
    Render the board as an SVG string (440×440 px).
    last_from / last_to squares are highlighted in translucent green.
    """
    parts: List[str] = []

    # ── SVG root ─────────────────────────────────────────────────────────────
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{_TOTAL_PX}" height="{_TOTAL_PX}" '
        f'viewBox="0 0 {_TOTAL_PX} {_TOTAL_PX}" '
        f'style="font-family:Arial,Helvetica,sans-serif;shape-rendering:crispEdges;">'
    )

    # ── Outer background ─────────────────────────────────────────────────────
    parts.append(
        f'<rect width="{_TOTAL_PX}" height="{_TOTAL_PX}" '
        f'fill="{_BG}" rx="6" ry="6"/>'
    )

    # ── Board squares ────────────────────────────────────────────────────────
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            x = _OFFSET + col * _CELL
            y = _OFFSET + row * _CELL
            fill = _LIGHT_SQ if (row + col) % 2 == 0 else _DARK_SQ
            parts.append(
                f'<rect x="{x}" y="{y}" '
                f'width="{_CELL}" height="{_CELL}" fill="{fill}"/>'
            )

    # ── Last-move highlights ─────────────────────────────────────────────────
    for sq in (last_from, last_to):
        if sq is not None:
            r, c = sq
            x = _OFFSET + c * _CELL
            y = _OFFSET + r * _CELL
            parts.append(
                f'<rect x="{x}" y="{y}" '
                f'width="{_CELL}" height="{_CELL}" '
                f'fill="{_HIGHLIGHT}" opacity="0.45"/>'
            )

    # ── Board border ─────────────────────────────────────────────────────────
    parts.append(
        f'<rect x="{_OFFSET}" y="{_OFFSET}" '
        f'width="{_BOARD_PX}" height="{_BOARD_PX}" '
        f'fill="none" stroke="#555555" stroke-width="1.5"/>'
    )

    # ── Pieces ───────────────────────────────────────────────────────────────
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            piece = board[row][col]
            if piece in (EMPTY_DARK, EMPTY_LIGHT):
                continue

            # Centre of the cell
            cx = _OFFSET + col * _CELL + _CELL // 2
            cy = _OFFSET + row * _CELL + _CELL // 2
            radius = _CELL // 2 - 5

            fill = _RED_FILL if _is_red(piece) else _BLUE_FILL

            # Drop shadow
            parts.append(
                f'<circle cx="{cx + 2}" cy="{cy + 2}" r="{radius}" '
                f'fill="rgba(0,0,0,0.35)"/>'
            )
            # Main piece body
            parts.append(
                f'<circle cx="{cx}" cy="{cy}" r="{radius}" '
                f'fill="{fill}" stroke="{_STROKE}" stroke-width="1.8"/>'
            )
            # Inner highlight ring (subtle gloss)
            parts.append(
                f'<circle cx="{cx}" cy="{cy}" r="{radius - 6}" '
                f'fill="none" stroke="rgba(255,255,255,0.25)" stroke-width="1.5"/>'
            )

            # King decoration — gold star
            if _is_king(piece):
                star_outer = radius - 10
                star_inner = star_outer // 2 + 1
                star_path  = _sq_star_path(cx, cy, star_outer, star_inner, points=5)
                parts.append(
                    f'<path d="{star_path}" '
                    f'fill="{_GOLD}" stroke="{_STROKE}" stroke-width="0.8" '
                    f'opacity="0.92"/>'
                )

    # ── Column labels A-H ────────────────────────────────────────────────────
    for i, lbl in enumerate("ABCDEFGH"):
        x = _OFFSET + i * _CELL + _CELL // 2
        # Top row
        parts.append(
            f'<text x="{x}" y="13" text-anchor="middle" dominant-baseline="middle" '
            f'font-size="11" font-weight="bold" fill="{_LABEL_CLR}">{lbl}</text>'
        )
        # Bottom row
        parts.append(
            f'<text x="{x}" y="{_OFFSET + _BOARD_PX + 11}" '
            f'text-anchor="middle" dominant-baseline="middle" '
            f'font-size="11" font-weight="bold" fill="{_LABEL_CLR}">{lbl}</text>'
        )

    # ── Row labels 1-8 (1 = bottom = index 7, 8 = top = index 0) ────────────
    for row_idx in range(BOARD_SIZE):
        display_num = 8 - row_idx           # row index 0 → label "8"
        y = _OFFSET + row_idx * _CELL + _CELL // 2
        # Left side
        parts.append(
            f'<text x="10" y="{y}" text-anchor="middle" dominant-baseline="middle" '
            f'font-size="11" font-weight="bold" fill="{_LABEL_CLR}">{display_num}</text>'
        )
        # Right side
        parts.append(
            f'<text x="{_OFFSET + _BOARD_PX + 10}" y="{y}" '
            f'text-anchor="middle" dominant-baseline="middle" '
            f'font-size="11" font-weight="bold" fill="{_LABEL_CLR}">{display_num}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


# ─── Endpoint Handlers ────────────────────────────────────────────────────────

async def _handle_new_game() -> NewGameResponse:
    """
    POST /new_game
    Initialise a fresh board and return the starting SVG.
    Red always moves first in standard checkers.
    """
    board = _init_board()
    svg   = _generate_svg(board)
    log.info("[checkers] New game started.")
    return NewGameResponse(
        board     = board,
        next_turn = RED,
        svg       = svg,
        game_over = False,
        winner    = None,
    )


async def _handle_move(req: MoveRequest) -> MoveResponse:
    """
    POST /move
    Validate and apply one player move, enforcing all standard checkers rules.

    Rules enforced:
      1. Player must own the piece at from_sq.
      2. Mandatory jump: if any capture exists the player MUST capture.
      3. Only forward movement for regular pieces; all directions for kings.
      4. Multi-jump: after a capture, if the same piece can jump again the
         turn does NOT switch — next_turn returns as the same player.
      5. King promotion after landing on the back rank.
      6. Win detection: no pieces left OR no legal moves.
    """
    board  = req.board
    player = req.turn.upper()

    # ── Basic player validation ───────────────────────────────────────────────
    if player not in (RED, BLUE):
        return MoveResponse(success=False, error="turn must be 'R' or 'B'.")

    # ── Parse coordinates ─────────────────────────────────────────────────────
    try:
        fr, fc = _parse_coord(req.from_sq)
        tr, tc = _parse_coord(req.to_sq)
    except ValueError as exc:
        return MoveResponse(success=False, error=str(exc))

    # ── Validate source square ────────────────────────────────────────────────
    if fr == tr and fc == tc:
        return MoveResponse(success=False, error="Source and destination are identical.")

    piece = board[fr][fc]
    if not _belongs_to(piece, player):
        return MoveResponse(
            success=False,
            error=f"Square {req.from_sq} does not contain a {player} piece.",
        )

    # ── Determine if this move is a jump ──────────────────────────────────────
    is_jump = abs(tr - fr) == 2

    # ── Mandatory-capture enforcement ────────────────────────────────────────
    board_captures = _all_captures(board, player)
    if board_captures and not is_jump:
        return MoveResponse(
            success=False,
            error=(
                "Mandatory jump rule: a capture is available — "
                "you must jump over an opponent's piece."
            ),
        )

    # ── Validate that the specific move appears in the legal-move list ────────
    if is_jump:
        legal = _captures_for_piece(board, fr, fc, player)
    else:
        legal = _simple_moves_for_piece(board, fr, fc, player)

    if (fr, fc, tr, tc) not in legal:
        return MoveResponse(
            success=False,
            error=f"'{req.from_sq}→{req.to_sq}' is not a legal move.",
        )

    # ── Apply the move ────────────────────────────────────────────────────────
    new_board, was_capture = _apply_move(board, fr, fc, tr, tc)

    # ── Multi-jump check ──────────────────────────────────────────────────────
    # If a capture was made and the landing piece can still jump, the SAME
    # player continues without the turn switching.
    multi_jump_pending = (
        was_capture
        and bool(_captures_for_piece(new_board, tr, tc, player))
    )

    next_player = player if multi_jump_pending else (BLUE if player == RED else RED)

    # ── Game-over check ───────────────────────────────────────────────────────
    game_over, winner = _check_game_over(new_board, next_player)

    # ── Generate updated SVG ──────────────────────────────────────────────────
    svg = _generate_svg(new_board, last_from=(fr, fc), last_to=(tr, tc))

    log.info(
        "[checkers] %s moved %s→%s | next=%s | game_over=%s | winner=%s",
        player, req.from_sq, req.to_sq, next_player, game_over, winner,
    )

    return MoveResponse(
        success   = True,
        board     = new_board,
        next_turn = next_player,
        svg       = svg,
        game_over = game_over,
        winner    = winner,
    )


# ─── Plugin Registration ──────────────────────────────────────────────────────

class CheckersPlugin(Plugin):
    """
    Checkers (Dama) game plugin.
    Exposes two endpoints consumed by the Go layer and Messenger bot:
      POST /new_game  — start a fresh game
      POST /move      — submit a player move
    """

    description  = "Checkers (Dama) game engine with dynamic SVG board generation"
    requirements: list[str] = []   # No external dependencies — pure stdlib + FastAPI/Pydantic
    pip_extra:    list[str] = []

    def name(self) -> str:
        return "checkers"

    def routes(self) -> list[Route]:
        return [
            Route(method="POST", path="/new_game", handler=_handle_new_game),
            Route(method="POST", path="/move",     handler=_handle_move),
        ]

    def status(self) -> dict:
        return {
            "status":    "loaded",
            "game":      "checkers",
            "endpoints": ["/new_game", "/move"],
        }


# ─── Required by loader — must be the last line ───────────────────────────────
plugin = CheckersPlugin()
