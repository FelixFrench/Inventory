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
from src.api.routers import groups, products, reports, scan, session
from src.api.routers.ws import (
    POLL_QUERY,
    REFRESH_POLL_QUERY,
    build_payload,
    build_refresh_notification,
    manager,
)
from src.api.routers.ws import router as ws_router
from src.db.db import get_connection
from src.version import __version__

import os
from dotenv import load_dotenv
load_dotenv(Path(__file__).parents[2] / "config.local.env")


logger = logging.getLogger(__name__)


def _compute_poll_updates(rows, last_seen: dict, retailer_id: int) -> list[str]:
    """Pure sync function. Computes which rows changed, mutates last_seen in-place,
    and returns a list of JSON strings ready to broadcast.

    ``last_seen`` is keyed by the composite ``(barcode, retailer_id)`` variant key, matching
    the session_items PK dimension (session_id is constant for the active session, so it is
    not part of the key). build_payload still receives the loop's scalar retailer_id
    unchanged."""
    payloads = []
    current_keys = set()
    for row in rows:
        key = (row["barcode"], row["retailer_id"])
        current_keys.add(key)
        new_status = (row["info_status"], row["price_status"])
        if last_seen.get(key) != new_status:
            payloads.append(json.dumps(build_payload("resolution", row, retailer_id)))
            last_seen[key] = new_status
    for k in list(last_seen.keys()):
        if k not in current_keys:
            del last_seen[k]
    return payloads


def _compute_refresh_updates(rows, last_seen_refresh: dict) -> list[str]:
    """Pure sync function for the 'refresh' broadcast (Sprint 2, Phase 3c). SEPARATE from
    _compute_poll_updates: it watches the durable lookup timestamps on the cache tables, never
    session_items, and does not touch the resolution/scan detection.

    ``last_seen_refresh`` is keyed by ``(barcode, retailer_id)`` with value ``(off_ts, price_ts)`` —
    the OFF (product_variants) and price (prices) ``last_lookup_datetime`` values, both possibly None.

    Emits a 'refresh' payload when a row's timestamps advance beyond the snapshot: a new key that
    already carries at least one non-NULL timestamp, or any later advance / NULL->non-NULL transition.
    A brand-new key whose timestamps are BOTH NULL (a never-resolved null-data row from set-minimum /
    group-add / a just-created refresh marker) is recorded as a silent baseline and does NOT broadcast
    — otherwise a FastAPI restart would emit a useless burst for every unstamped row. Rows whose
    timestamps are unchanged (including rows that stay NULL) broadcast nothing. Removed rows are pruned.

    Fires for ANY session-less durable write — both manual refresh (3c) and the 3b background
    scheduler; clients filter by their own barcode, so background broadcasts are cheap and harmless.
    """
    payloads = []
    current_keys = set()
    for row in rows:
        key = (row["barcode"], row["retailer_id"])
        current_keys.add(key)
        new_ts = (row["off_ts"], row["price_ts"])
        if key not in last_seen_refresh:
            # New key: emit only if something has actually been stamped; else record a silent baseline.
            if new_ts != (None, None):
                payloads.append(json.dumps(build_refresh_notification(row)))
            last_seen_refresh[key] = new_ts
        elif last_seen_refresh[key] != new_ts:
            payloads.append(json.dumps(build_refresh_notification(row)))
            last_seen_refresh[key] = new_ts
    for k in list(last_seen_refresh.keys()):
        if k not in current_keys:
            del last_seen_refresh[k]
    return payloads


async def _poll_tick(rows, last_seen: dict, retailer_id: int) -> None:
    """Async wrapper: broadcasts all changed payloads from one poll tick."""
    for payload in _compute_poll_updates(rows, last_seen, retailer_id):
        await manager.broadcast(payload)


async def _refresh_tick(rows, last_seen_refresh: dict) -> None:
    """Async wrapper: broadcasts all 'refresh' payloads from one poll tick."""
    for payload in _compute_refresh_updates(rows, last_seen_refresh):
        await manager.broadcast(payload)


async def _session_poll_loop(retailer_id: int) -> None:
    last_seen: dict[tuple[str, int], tuple[str, str]] = {}
    last_seen_refresh: dict[tuple[str, int], tuple[str | None, str | None]] = {}
    conn = get_connection()
    try:
        while True:
            try:
                rows = conn.execute(POLL_QUERY, (retailer_id, retailer_id)).fetchall()
                await _poll_tick(rows, last_seen, retailer_id)
                # Separate refresh-detection pass on the same connection (3c). Independent of the
                # session_items resolution detection above; watches the durable cache timestamps.
                refresh_rows = conn.execute(REFRESH_POLL_QUERY).fetchall()
                await _refresh_tick(refresh_rows, last_seen_refresh)
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
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(scan.router,    dependencies=[Depends(verify_api_key)])
app.include_router(session.router, dependencies=[Depends(verify_api_key)])
app.include_router(reports.router,   dependencies=[Depends(verify_api_key)])
app.include_router(products.router,  dependencies=[Depends(verify_api_key)])
app.include_router(groups.router,    dependencies=[Depends(verify_api_key)])
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
