import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request

from src.api.models import ScanRequest, ScanResponse
from src.api.routers.ws import build_payload, manager
from src.db.db import get_connection

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Scanning"])

_503 = HTTPException(
    status_code=503,
    detail="Service temporarily unavailable",
    headers={"Retry-After": "1"},
)


def _do_scan(barcode: str, retailer_id: int) -> dict:
    """Run the full scan DB transaction. Returns scan result dict or raises HTTPException."""
    try:
        conn = get_connection()
        try:
            session_row = conn.execute(
                "SELECT id, type FROM sessions LIMIT 1"
            ).fetchone()
            if session_row is None:
                raise HTTPException(status_code=409, detail={"error": "no_active_session"})

            session_id = session_row["id"]
            now = datetime.now(UTC).isoformat()

            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)",
                    (barcode,),
                )

                check = conn.execute(
                    """
                    SELECT
                        pv.name, pv.brand, pv.product_quantity, pr.price_pence, pr.product_url,
                        (pv.barcode IS NOT NULL) AS has_info,
                        (pr.barcode IS NOT NULL) AS has_price,
                        COALESCE(inv.quantity, 0) AS inventory_quantity
                    FROM barcodes b
                    LEFT JOIN product_variants pv
                           ON pv.barcode = b.barcode AND pv.retailer_id = ?
                    LEFT JOIN prices pr
                           ON pr.barcode = b.barcode AND pr.retailer_id = ?
                    LEFT JOIN inventory inv
                           ON inv.barcode = b.barcode
                    WHERE b.barcode = ?
                    """,
                    (retailer_id, retailer_id, barcode),
                ).fetchone()

                has_info = bool(check["has_info"])
                has_price = bool(check["has_price"])

                if has_info and has_price:
                    info_status = "resolved"
                    price_status = "resolved"
                elif has_info:
                    info_status = "resolved"
                    price_status = "pending"
                else:
                    info_status = "pending"
                    price_status = "pending"

                conn.execute(
                    """
                    INSERT INTO session_items
                        (session_id, barcode, delta, first_scanned_at, info_status, price_status)
                    VALUES (?, ?, 1, ?, ?, ?)
                    ON CONFLICT(session_id, barcode) DO UPDATE SET delta = delta + 1
                    """,
                    (session_id, barcode, now, info_status, price_status),
                )

                delta = conn.execute(
                    "SELECT delta FROM session_items WHERE session_id = ? AND barcode = ?",
                    (session_id, barcode),
                ).fetchone()["delta"]

            return {
                "barcode": barcode,
                "session_delta": delta,
                "info_status": info_status,
                "price_status": price_status,
                "name": check["name"],
                "brand": check["brand"],
                "product_quantity": check["product_quantity"],
                "price_pence": check["price_pence"],
                "product_url": check["product_url"],
                "inventory_quantity": check["inventory_quantity"],
            }
        finally:
            conn.close()
    except HTTPException:
        raise
    except sqlite3.OperationalError:
        raise _503


@router.post("/scan", response_model=ScanResponse)
async def scan(body: ScanRequest, request: Request) -> ScanResponse:
    """
    Record a barcode scan against the active session.

    Increments the session delta for the barcode by 1 (or creates the item if first
    scan). Broadcasts a WebSocket message to all connected clients. Returns 409 if no
    session is currently active, 503 on transient database contention.
    """
    retailer_id = request.app.state.sainsburys_retailer_id
    result = await asyncio.to_thread(_do_scan, body.barcode, retailer_id)
    payload = build_payload("scan", result)
    payload["inventory_quantity"] = result["inventory_quantity"]
    await manager.broadcast(json.dumps(payload))
    return ScanResponse(barcode=result["barcode"], session_delta=result["session_delta"])
