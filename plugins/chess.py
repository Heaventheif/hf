"""
plugins/chess.py — Plugin الشطرنج.

يوفر endpoint واحداً:
  POST /process_move — يستقبل FEN + حركة، يعيد FEN جديد + صورة PNG base64.

تسلسل محرك النقلات:
  ① Lichess Cloud Eval  (مجاني، لا يحتاج مفتاح)
  ② Stockfish المحلي   (إذا كان مثبَّتاً)
  ③ نقلة عشوائية قانونية (خطة احتياطية)

إضافات البنية التحتية المطلوبة في Dockerfile:
  apt-get install stockfish libcairo2 libpango-1.0-0 libpangocairo-1.0-0
  pip: chess cairosvg
"""
from __future__ import annotations

import base64
import logging
import os
import random
import time
from typing import Any, Optional

import chess
import chess.engine
import chess.svg
import cairosvg
import requests
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from internal.plugin import Plugin, Route

log = logging.getLogger(__name__)

# ─── إعدادات ─────────────────────────────────────────────────────────────────

LICHESS_CLOUD_URL = "https://lichess.org/api/cloud-eval"
STOCKFISH_PATH    = os.environ.get("STOCKFISH_PATH", "stockfish")
LICHESS_TOKEN     = os.environ.get("LICHESS_TOKEN", "")   # اختياري
BOARD_SIZE        = int(os.environ.get("BOARD_SIZE", "480"))

# difficulty (1-20) → عمق Stockfish
_DEPTH = {1:2, 2:3, 3:4, 4:5, 5:6, 6:7, 7:8, 8:9, 9:12, 10:15, 15:18, 20:22}

# ألوان رقعة الشطرنج (chess.svg.DEFAULT_COLORS في v1.11+)
_COLORS = chess.svg.DEFAULT_COLORS.copy()
_COLORS.update({
    "square light":          "#F0D9B5",
    "square dark":           "#B58863",
    "square light lastmove": "#CDD26A",
    "square dark lastmove":  "#AABA44",
    "margin":                "#2B2B2B",
    "coord":                 "#E0E0E0",
})


# ─── نماذج Pydantic ──────────────────────────────────────────────────────────

class MoveRequest(BaseModel):
    fen:        str
    move:       Optional[str] = None   # UCI مثل "e2e4" — None لرسم الرقعة فقط
    bot_mode:   bool = False           # هل البوت يرد تلقائياً؟
    difficulty: int  = 10             # 1–20
    perspective: str = "white"        # "white" | "black"


# ─── رسم الرقعة ──────────────────────────────────────────────────────────────

def _board_png_b64(
    board:       chess.Board,
    perspective: str = "white",
    last_move:   Optional[chess.Move] = None,
) -> str:
    """يحوّل رقعة الشطرنج إلى PNG base64 جاهزة للإرسال عبر فيسبوك."""
    flipped     = perspective == "black"
    check_sq    = board.king(board.turn) if board.is_check() else None
    svg_str     = chess.svg.board(
        board,
        flipped     = flipped,
        lastmove    = last_move,
        check       = check_sq,
        colors      = _COLORS,
        size        = BOARD_SIZE,
        coordinates = True,
        borders     = True,
    )
    png = cairosvg.svg2png(
        bytestring    = svg_str.encode("utf-8"),
        output_width  = BOARD_SIZE,
        output_height = BOARD_SIZE,
    )
    return base64.b64encode(png).decode("utf-8")


# ─── محرك النقلات ────────────────────────────────────────────────────────────

def _lichess_move(fen: str) -> Optional[str]:
    """① Lichess Cloud Eval — مجاني، لا يحتاج تثبيتاً."""
    try:
        headers = {"Accept": "application/json"}
        if LICHESS_TOKEN:
            headers["Authorization"] = f"Bearer {LICHESS_TOKEN}"
        r = requests.get(
            LICHESS_CLOUD_URL,
            params  = {"fen": fen, "multiPv": 1, "variant": "standard"},
            headers = headers,
            timeout = 6,
        )
        if r.ok:
            moves = r.json().get("pvs", [{}])[0].get("moves", "").split()
            if moves:
                log.info("[chess:lichess] %s", moves[0])
                return moves[0]
    except Exception as e:
        log.debug("[chess:lichess] %s", e)
    return None


def _stockfish_move(fen: str, difficulty: int) -> Optional[str]:
    """② Stockfish المحلي — يحتاج `apt install stockfish` في Dockerfile."""
    depth = _DEPTH.get(difficulty, 10)
    try:
        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as eng:
            board  = chess.Board(fen)
            result = eng.play(board, chess.engine.Limit(depth=depth, time=min(difficulty * 0.15, 4.0)))
            if result.move:
                log.info("[chess:stockfish] depth=%d → %s", depth, result.move.uci())
                return result.move.uci()
    except FileNotFoundError:
        log.warning("[chess:stockfish] لم يُعثر على stockfish في PATH")
    except Exception as e:
        log.debug("[chess:stockfish] %s", e)
    return None


def _get_engine_move(fen: str, difficulty: int) -> Optional[str]:
    """يجرب المصادر بالترتيب ويُعيد أفضل نقلة متاحة."""
    uci = _lichess_move(fen) or _stockfish_move(fen, difficulty)
    if uci:
        return uci
    # ③ احتياطي عشوائي — دائماً يعمل
    legal = list(chess.Board(fen).legal_moves)
    if legal:
        chosen = random.choice(legal).uci()
        log.warning("[chess:fallback] نقلة عشوائية: %s", chosen)
        return chosen
    return None


# ─── دوال مساعدة ─────────────────────────────────────────────────────────────

def _winner_label(result: str) -> Optional[str]:
    return {"1-0": "أبيض", "0-1": "أسود"}.get(result)


def _fix_promotion(board: chess.Board, uci: str) -> chess.Move:
    """يُضيف ترقية وزير تلقائياً إذا وصل البيدق للصف الأخير بدون حرف."""
    move = chess.Move.from_uci(uci.lower())
    if len(uci) == 4:
        piece = board.piece_at(move.from_square)
        if piece and piece.piece_type == chess.PAWN:
            rank = chess.square_rank(move.to_square)
            if (piece.color == chess.WHITE and rank == 7) or \
               (piece.color == chess.BLACK and rank == 0):
                move = chess.Move(move.from_square, move.to_square, chess.QUEEN)
    return move


# ─── Handler ─────────────────────────────────────────────────────────────────

async def _handle_process_move(req: MoveRequest) -> JSONResponse:
    """
    POST /process_move
    يعالج نقلة اللاعب ثم نقلة البوت (إذا bot_mode=True).
    يُعيد JSON متوافق مع chess.js.
    """
    t0 = time.time()

    # ── تحليل FEN ────────────────────────────────────────────────────────────
    try:
        board = chess.Board(req.fen)
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "FEN غير صالح"})

    last_move: Optional[chess.Move] = None

    # ── تطبيق نقلة اللاعب ────────────────────────────────────────────────────
    if req.move:
        raw = req.move.strip()
        try:
            move = _fix_promotion(board, raw)
            if move not in board.legal_moves:
                return JSONResponse({
                    "illegal_move_error":
                        f"❌ نقلة غير قانونية: {raw.upper()}\n"
                        f"مثال صحيح: e2e4 أو e7e8q"
                })
            board.push(move)
            last_move = move
        except ValueError:
            return JSONResponse({
                "illegal_move_error":
                    f"❌ صيغة خاطئة: {raw.upper()}\n"
                    f"استخدم صيغة UCI مثل: e2e4"
            })

    # ── فحص نهاية اللعبة بعد نقلة اللاعب ────────────────────────────────────
    if board.is_game_over():
        winner = _winner_label(board.result())
        log.info("[chess] game_over winner=%s (%.2fs)", winner, time.time()-t0)
        return JSONResponse({
            "new_fen":      board.fen(),
            "image_base64": _board_png_b64(board, req.perspective, last_move),
            "game_over":    True,
            "winner":       winner,
        })

    # ── نقلة البوت ───────────────────────────────────────────────────────────
    if req.bot_mode and req.move:
        bot_uci = _get_engine_move(board.fen(), req.difficulty)
        if bot_uci:
            try:
                bot_move = _fix_promotion(board, bot_uci)
                if bot_move in board.legal_moves:
                    board.push(bot_move)
                    last_move = bot_move
            except Exception as e:
                log.error("[chess:bot] خطأ: %s", e)

    # ── النتيجة النهائية ─────────────────────────────────────────────────────
    game_over = board.is_game_over()
    winner    = _winner_label(board.result()) if game_over else None

    log.info("[chess] done game_over=%s (%.2fs)", game_over, time.time()-t0)
    return JSONResponse({
        "new_fen":      board.fen(),
        "image_base64": _board_png_b64(board, req.perspective, last_move),
        "game_over":    game_over,
        "winner":       winner,
    })


# ─── Plugin ──────────────────────────────────────────────────────────────────

class ChessPlugin(Plugin):
    """Plugin الشطرنج — يوفر POST /process_move."""

    description  = "محرك شطرنج رسومي (Lichess + Stockfish) — صور PNG لشات فيسبوك"
    requirements = [
        "chess>=1.10.0",
        "cairosvg>=2.7.1",
        "requests>=2.28.0",
    ]
    # Cairo تحتاج حزم نظام في Dockerfile — انظر التعليق أعلى الملف

    def name(self) -> str:
        return "chess"

    def routes(self) -> list[Route]:
        return [Route(method="POST", path="/process_move", handler=_handle_process_move)]

    def startup(self) -> None:
        # اختبار سريع للتأكد من أن chess يعمل
        board = chess.Board()
        board.push(chess.Move.from_uci("e2e4"))
        log.info("✅ [chess] جاهز — python-chess %s", chess.__version__)

    def status(self) -> dict:
        sf_ok = False
        try:
            with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as e:
                sf_ok = True
        except Exception:
            pass
        return {
            "status":    "loaded",
            "stockfish": "✅" if sf_ok else "⚠️ غير متاح (Lichess + fallback يعملان)",
            "board_size": BOARD_SIZE,
        }


plugin = ChessPlugin()
