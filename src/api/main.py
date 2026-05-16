import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from src.api.routers import mode, scan
from src.db.db import get_connection

logger = logging.getLogger(__name__)

app = FastAPI()
app.include_router(scan.router)
app.include_router(mode.router)


@app.on_event("startup")
async def startup():
    alembic_cfg = Config(str(Path.home() / "repos" / "Inventory" / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")

    conn = get_connection()
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        logger.info("journal_mode: %s", row[0])
    finally:
        conn.close()
    logger.info("Inventory FastAPI started")
