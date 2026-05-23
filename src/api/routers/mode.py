import logging
import sqlite3

from fastapi import APIRouter, Depends, WebSocket

from src.api.dependencies import get_db
from src.api.models import ModeRequest, ModeResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/mode", response_model=ModeResponse)
def get_mode(db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT value FROM config WHERE key = 'scan_mode'").fetchone()
    return ModeResponse(mode=row["value"] if row else "out")


@router.post("/mode", response_model=ModeResponse)
def set_mode(body: ModeRequest, db: sqlite3.Connection = Depends(get_db)):
    with db:
        db.execute(
            "INSERT INTO config (key, value) VALUES ('scan_mode', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (body.mode,),
        )
    return ModeResponse(mode=body.mode)


@router.websocket("/ws")
async def websocket_stub(websocket: WebSocket):
    await websocket.accept()
    await websocket.close()
