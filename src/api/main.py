import logging
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from src.api.dependencies import verify_api_key
from src.api.routers import mode, scan, reports, ws
from src.db.db import get_connection

logger = logging.getLogger(__name__)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' ws:"
        )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    _PROJECT_ROOT = Path(__file__).parents[2]
    alembic_cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")

    conn = get_connection()
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        logger.info("journal_mode: %s", row[0])
    finally:
        conn.close()
    logger.info("Inventory FastAPI started")
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(scan.router,    dependencies=[Depends(verify_api_key)])
app.include_router(mode.router,    dependencies=[Depends(verify_api_key)])
app.include_router(reports.router, dependencies=[Depends(verify_api_key)])
# No Depends(verify_api_key) — intentional. Browser WS API cannot send custom
# headers; LAN-only deployment makes unauthenticated /ws acceptable for this spike.
# SPIKE A — REMOVE BEFORE PHASE 2
app.include_router(ws.router)

# Must remain after all include_router() calls — FastAPI matches routes in
# registration order and this catch-all would shadow any router added after it.
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
