import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

import src.api.dependencies as _api_deps
from src.api.dependencies import verify_api_key, verify_docs_access
from src.api.models import DocsLoginRequest
from src.api.routers import printing as print_router
from src.api.routers import products, reports, scan, session
from src.api.routers.ws import POLL_QUERY, build_payload, manager
from src.api.routers.ws import router as ws_router
from src.db.db import get_connection

import os
from dotenv import load_dotenv
load_dotenv(Path(__file__).parents[2] / "config.local.env")


logger = logging.getLogger(__name__)


def _compute_poll_updates(rows, last_seen: dict) -> list[str]:
    """Pure sync function. Computes which rows changed, mutates last_seen in-place,
    and returns a list of JSON strings ready to broadcast."""
    payloads = []
    current_barcodes = set()
    for row in rows:
        barcode = row["barcode"]
        current_barcodes.add(barcode)
        new_status = (row["info_status"], row["price_status"])
        if last_seen.get(barcode) != new_status:
            payloads.append(json.dumps(build_payload("resolution", row)))
            last_seen[barcode] = new_status
    for b in list(last_seen.keys()):
        if b not in current_barcodes:
            del last_seen[b]
    return payloads


async def _poll_tick(rows, last_seen: dict) -> None:
    """Async wrapper: broadcasts all changed payloads from one poll tick."""
    for payload in _compute_poll_updates(rows, last_seen):
        await manager.broadcast(payload)


async def _session_poll_loop(retailer_id: int) -> None:
    last_seen: dict[str, tuple[str, str]] = {}
    conn = get_connection()
    try:
        while True:
            try:
                rows = conn.execute(POLL_QUERY, (retailer_id, retailer_id)).fetchall()
                await _poll_tick(rows, last_seen)
            except Exception:
                logger.exception("Poll loop tick failed")
            await asyncio.sleep(1)
    finally:
        conn.close()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path == "/docs":
            csp = (
                "default-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
            )
        else:
            csp = (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' ws:"
            )
        response.headers["Content-Security-Policy"] = csp
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    _PROJECT_ROOT = Path(__file__).parents[2]
    alembic_cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")
    if os.environ.get("INVENTORY_API_KEY") is None:
       raise RuntimeError("INVENTORY_API_KEY not set in environment or config.local.env")


    conn = get_connection()
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        logger.info("journal_mode: %s", row[0])
    finally:
        conn.close()

    with get_connection() as conn:
        row = conn.execute("SELECT id FROM sessions LIMIT 1").fetchone()
        if row:
            conn.execute(
                "UPDATE sessions SET recovered_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), row["id"]),
            )
            logger.info(f"Session {row['id']} recovered after restart")

    with get_connection() as conn:
        retailer = conn.execute(
            "SELECT id FROM retailers WHERE name = 'Sainsbury''s'"
        ).fetchone()
        if retailer is None:
            raise RuntimeError("Sainsbury's retailer not configured")
        app.state.sainsburys_retailer_id = retailer["id"]

    logger.info("Inventory FastAPI started")
    task = asyncio.create_task(_session_poll_loop(app.state.sainsburys_retailer_id))
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    lifespan=lifespan,
    title="Inventory API",
    version="1.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(scan.router,    dependencies=[Depends(verify_api_key)])
app.include_router(session.router, dependencies=[Depends(verify_api_key)])
app.include_router(reports.router,   dependencies=[Depends(verify_api_key)])
app.include_router(products.router,  dependencies=[Depends(verify_api_key)])
app.include_router(print_router.router, dependencies=[Depends(verify_api_key)])
app.include_router(ws_router)


@app.get("/")
async def root():
    return RedirectResponse(url="/feed.html")


@app.post("/docs-login", include_in_schema=False)
async def docs_login(body: DocsLoginRequest, response: Response) -> dict:
    """Exchange a valid API key for a short-lived docs session cookie."""
    if _api_deps._API_KEY is None:
        raise HTTPException(status_code=500, detail="Server misconfigured")
    if not secrets.compare_digest(body.api_key, _api_deps._API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")
    response.set_cookie(
        key="docs_session",
        value=body.api_key,
        httponly=True,
        samesite="strict",
        max_age=28800,
        path="/docs",
    )
    return {"ok": True}


@app.get("/docs", response_class=HTMLResponse, include_in_schema=False)
async def get_swagger_docs(_: None = Depends(verify_docs_access)):
    """Serve the Swagger UI. Requires a valid X-API-Key header or docs session cookie."""
    return """<!DOCTYPE html>
<html>
  <head>
    <title>Inventory API</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" href="/swagger-ui/swagger-ui.css">
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="/swagger-ui/swagger-ui-bundle.js"></script>
    <script src="/swagger-ui/swagger-init.js"></script>
  </body>
</html>"""


@app.get("/openapi.json", include_in_schema=False)
async def get_openapi_schema():
    """Serve the OpenAPI schema JSON."""
    return JSONResponse(app.openapi())


# Must remain after all include_router() calls — FastAPI matches routes in
# registration order and this catch-all would shadow any router added after it.
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
