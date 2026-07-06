"""
plugins/chess.py
بوت الشطرنج
"""
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from bot_chess.chess_engine import MoveRequest, MoveResponse, apply_move_and_get_response
import chess

DESCRIPTION     = "بوت الشطرنج — تحليل الحركات والرد"
DOCKERFILE_DEPS = []


def register(app):

    @app.post("/process_move", response_model=MoveResponse, tags=["chess"])
    async def process_move(req: MoveRequest):
        try:
            chess.Board(req.fen)
        except ValueError:
            raise HTTPException(400, "Invalid FEN string")
        # ← إصلاح: apply_move_and_get_response تشغّل minimax وتحويل
        # SVG→PNG (cairosvg) — عمليات CPU ثقيلة sync. استدعاؤها مباشرة
        # داخل async def يجمّد الـ event loop الوحيد بالسيرفر (worker=1)
        # لحد ما تخلص، فيتوقف الرد على كل الطلبات التانية (pin/yt/gemini...)
        # بالمدة هذه. تشغيلها بـ threadpool يخلي باقي الطلبات تستمر بالتوازي.
        return await run_in_threadpool(
            apply_move_and_get_response, req.fen, req.move, req.bot_mode, req.difficulty
        )
