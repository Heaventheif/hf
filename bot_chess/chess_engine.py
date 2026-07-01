# bot_chess/chess_engine.py
import base64
import chess
import chess.svg
import cairosvg
import random
import math
from typing import Optional
from pydantic import BaseModel


class MoveRequest(BaseModel):
    fen: str
    move: Optional[str] = None
    bot_mode: bool = False
    difficulty: int = 3


class MoveResponse(BaseModel):
    new_fen: str
    image_base64: str
    game_over: bool = False
    winner: Optional[str] = None
    illegal_move_error: Optional[str] = None


# ═══════════════════════════════════════════════════════════════
# جداول تقييم المواقع
# ═══════════════════════════════════════════════════════════════

PIECE_VALUES = {
    chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
    chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 20000,
}

PAWN_TABLE = [
     0,  0,  0,  0,  0,  0,  0,  0,
    50, 50, 50, 50, 50, 50, 50, 50,
    10, 10, 20, 30, 30, 20, 10, 10,
     5,  5, 10, 25, 25, 10,  5,  5,
     0,  0,  0, 20, 20,  0,  0,  0,
     5, -5,-10,  0,  0,-10, -5,  5,
     5, 10, 10,-20,-20, 10, 10,  5,
     0,  0,  0,  0,  0,  0,  0,  0,
]
KNIGHT_TABLE = [
    -50,-40,-30,-30,-30,-30,-40,-50,
    -40,-20,  0,  0,  0,  0,-20,-40,
    -30,  0, 10, 15, 15, 10,  0,-30,
    -30,  5, 15, 20, 20, 15,  5,-30,
    -30,  0, 15, 20, 20, 15,  0,-30,
    -30,  5, 10, 15, 15, 10,  5,-30,
    -40,-20,  0,  5,  5,  0,-20,-40,
    -50,-40,-30,-30,-30,-30,-40,-50,
]
BISHOP_TABLE = [
    -20,-10,-10,-10,-10,-10,-10,-20,
    -10,  0,  0,  0,  0,  0,  0,-10,
    -10,  0,  5, 10, 10,  5,  0,-10,
    -10,  5,  5, 10, 10,  5,  5,-10,
    -10,  0, 10, 10, 10, 10,  0,-10,
    -10, 10, 10, 10, 10, 10, 10,-10,
    -10,  5,  0,  0,  0,  0,  5,-10,
    -20,-10,-10,-10,-10,-10,-10,-20,
]
ROOK_TABLE = [
     0,  0,  0,  0,  0,  0,  0,  0,
     5, 10, 10, 10, 10, 10, 10,  5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
    -5,  0,  0,  0,  0,  0,  0, -5,
     0,  0,  0,  5,  5,  0,  0,  0,
]
KING_TABLE = [
    -30,-40,-40,-50,-50,-40,-40,-30,
    -30,-40,-40,-50,-50,-40,-40,-30,
    -30,-40,-40,-50,-50,-40,-40,-30,
    -30,-40,-40,-50,-50,-40,-40,-30,
    -20,-30,-30,-40,-40,-30,-30,-20,
    -10,-20,-20,-20,-20,-20,-20,-10,
     20, 20,  0,  0,  0,  0, 20, 20,
     20, 30, 10,  0,  0, 10, 30, 20,
]

PIECE_TABLES = {
    chess.PAWN:   PAWN_TABLE,
    chess.KNIGHT: KNIGHT_TABLE,
    chess.BISHOP: BISHOP_TABLE,
    chess.ROOK:   ROOK_TABLE,
    chess.KING:   KING_TABLE,
}


# ═══════════════════════════════════════════════════════════════
# تقييم الرقعة
# ═══════════════════════════════════════════════════════════════

def evaluate_board(board: chess.Board) -> int:
    if board.is_checkmate():
        return -99999 if board.turn == chess.WHITE else 99999
    if board.is_stalemate() or board.is_insufficient_material():
        return 0

    score = 0
    for square in chess.SQUARES:
        piece = board.piece_at(square)
        if piece is None:
            continue
        value = PIECE_VALUES.get(piece.piece_type, 0)
        table = PIECE_TABLES.get(piece.piece_type)
        if table:
            idx = square if piece.color == chess.WHITE else chess.square_mirror(square)
            value += table[idx]
        score += value if piece.color == chess.WHITE else -value
    return score


# ═══════════════════════════════════════════════════════════════
# Minimax + Alpha-Beta
# ═══════════════════════════════════════════════════════════════

def minimax(board: chess.Board, depth: int, alpha: int, beta: int, maximizing: bool) -> int:
    if depth == 0 or board.is_game_over():
        return evaluate_board(board)

    if maximizing:
        max_eval = -math.inf
        for move in board.legal_moves:
            board.push(move)
            max_eval = max(max_eval, minimax(board, depth-1, alpha, beta, False))
            board.pop()
            alpha = max(alpha, max_eval)
            if beta <= alpha:
                break
        return max_eval
    else:
        min_eval = math.inf
        for move in board.legal_moves:
            board.push(move)
            min_eval = min(min_eval, minimax(board, depth-1, alpha, beta, True))
            board.pop()
            beta = min(beta, min_eval)
            if beta <= alpha:
                break
        return min_eval


# ═══════════════════════════════════════════════════════════════
# مستويات الصعوبة: (عمق البحث، نطاق الضوضاء العشوائية)
# ═══════════════════════════════════════════════════════════════

DIFFICULTY_LEVELS = {
    1: {"depth": 1, "noise": 100},  # مبتدئ جداً
    2: {"depth": 2, "noise": 50},   # مبتدئ
    3: {"depth": 3, "noise": 15},   # متوسط (الافتراضي السابق)
    4: {"depth": 4, "noise": 5},    # متقدم
    5: {"depth": 5, "noise": 0},    # صعب جداً
}

DEFAULT_DIFFICULTY = 3


def _resolve_difficulty(level: int) -> dict:
    return DIFFICULTY_LEVELS.get(level, DIFFICULTY_LEVELS[DEFAULT_DIFFICULTY])


# ═══════════════════════════════════════════════════════════════
# المحرك الهجين — الإصلاح الرئيسي هنا
# ═══════════════════════════════════════════════════════════════

def hybrid_engine(board: chess.Board, difficulty: int = DEFAULT_DIFFICULTY) -> chess.Move:
    """
    محرك هجين Goatv2 + Aurora.
    الإصلاح: نحفظ board.turn قبل push، لا نقرأه بعده.
    difficulty: 1 (أسهل) إلى 5 (أصعب) — يتحكم في عمق البحث ومقدار الضوضاء.
    """
    legal_moves = list(board.legal_moves)
    if not legal_moves:
        return None

    # نحفظ لون اللاعب الحالي قبل أي push
    current_color = board.turn
    is_maximizing = (current_color == chess.WHITE)

    level = _resolve_difficulty(difficulty)
    depth = level["depth"]
    noise_range = level["noise"]

    best_move  = None
    best_score = -math.inf if is_maximizing else math.inf

    for move in legal_moves:
        board.push(move)
        # بعد push يصبح دور الخصم → نمرر عكس is_maximizing
        score = minimax(board, depth-1, -math.inf, math.inf, not is_maximizing)
        board.pop()
        if noise_range:
            score += random.randint(-noise_range, noise_range)

        if is_maximizing:
            if score > best_score:
                best_score = score
                best_move  = move
        else:
            if score < best_score:
                best_score = score
                best_move  = move

    return best_move or random.choice(legal_moves)


# ═══════════════════════════════════════════════════════════════
# توليد الصورة
# ═══════════════════════════════════════════════════════════════

def fen_to_png_base64(board: chess.Board, last_move: chess.Move = None) -> str:
    arrows = []
    if last_move:
        arrows = [chess.svg.Arrow(
            last_move.from_square, last_move.to_square,
            color="#00cc44cc"
        )]
    # تمييز الملك إذا كان في كش
    check_square = None
    if board.is_check():
        check_square = board.king(board.turn)

    svg_data = chess.svg.board(
        board=board,
        arrows=arrows,
        squares=chess.SquareSet([check_square]) if check_square is not None else None,
        size=420,
        coordinates=True,
    )
    png_bytes = cairosvg.svg2png(bytestring=svg_data.encode("utf-8"))
    return base64.b64encode(png_bytes).decode("utf-8")


# ═══════════════════════════════════════════════════════════════
# الدالة الرئيسية
# ═══════════════════════════════════════════════════════════════

def apply_move_and_get_response(
    fen: str, move_uci: Optional[str], bot_mode: bool, difficulty: int = DEFAULT_DIFFICULTY
) -> MoveResponse:
    board     = chess.Board(fen)
    last_move = None

    # ─── تطبيق نقلة المستخدم ─────────────────────────────────
    if move_uci:
        try:
            move = chess.Move.from_uci(move_uci)
            if move not in board.legal_moves:
                # ترقية تلقائية إلى وزير
                promo = chess.Move.from_uci(move_uci[:4] + "q")
                if promo in board.legal_moves:
                    move = promo
                else:
                    return MoveResponse(
                        new_fen=fen,
                        image_base64=fen_to_png_base64(board),
                        illegal_move_error="❌ نقلة غير قانونية! يرجى مراجعة الرقعة والمحاولة مجدداً",
                    )
            board.push(move)
            last_move = move
        except Exception:
            return MoveResponse(
                new_fen=fen,
                image_base64=fen_to_png_base64(board),
                illegal_move_error="❌ صيغة النقلة غير صحيحة! استخدم UCI مثل: e2e4",
            )

    # ─── فحص انتهاء اللعبة بعد نقلة المستخدم ────────────────
    if board.is_game_over():
        winner = None
        if board.is_checkmate():
            winner = "أبيض" if board.turn == chess.BLACK else "أسود"
        return MoveResponse(
            new_fen=board.fen(), image_base64=fen_to_png_base64(board, last_move),
            game_over=True, winner=winner,
        )

    # ─── نقلة البوت ──────────────────────────────────────────
    if bot_mode:
        bot_move = hybrid_engine(board, difficulty)
        if bot_move:
            board.push(bot_move)
            last_move = bot_move

    # ─── فحص انتهاء اللعبة بعد نقلة البوت ───────────────────
    if board.is_game_over():
        winner = None
        if board.is_checkmate():
            winner = "أبيض" if board.turn == chess.BLACK else "أسود"
        return MoveResponse(
            new_fen=board.fen(), image_base64=fen_to_png_base64(board, last_move),
            game_over=True, winner=winner,
        )

    return MoveResponse(
        new_fen=board.fen(),
        image_base64=fen_to_png_base64(board, last_move),
    )
