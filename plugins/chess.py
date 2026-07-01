"""
plugins/chess.py
بوت الشطرنج
"""
from fastapi import HTTPException
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
        return apply_move_and_get_response(req.fen, req.move, req.bot_mode, req.difficulty)
