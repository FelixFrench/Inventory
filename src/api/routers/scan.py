import logging
import sqlite3
from datetime import datetime, UTC

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import get_retailer_id
from src.api.models import ScanRequest, ScanResponse
from src.db.db import get_connection

logger = logging.getLogger(__name__)
router = APIRouter()

_503 = HTTPException(
    status_code=503,
    detail="Service temporarily unavailable",
    headers={"Retry-After": "1"},
)


@router.post("/scan", response_model=ScanResponse)
def scan(body: ScanRequest, retailer_id: int = Depends(get_retailer_id)):
    try:
        conn = get_connection()
        try:
            session_row = conn.execute(
                "SELECT id, type FROM sessions LIMIT 1"
            ).fetchone()
            if session_row is None:
                raise HTTPException(status_code=409, detail={"error": "no_active_session"})

            session_id = session_row['id']
            now = datetime.now(UTC).isoformat()

            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)",
                    (body.barcode,)
                )

                check = conn.execute(
                    """
                    SELECT
                        EXISTS(SELECT 1 FROM product_variants WHERE barcode = ? AND retailer_id = ?) AS has_info,
                        EXISTS(SELECT 1 FROM prices WHERE barcode = ? AND retailer_id = ?) AS has_price
                    """,
                    (body.barcode, retailer_id, body.barcode, retailer_id)
                ).fetchone()

                has_info = bool(check['has_info'])
                has_price = bool(check['has_price'])

                if has_info and has_price:
                    info_status = 'resolved'
                    price_status = 'resolved'
                elif has_info:
                    info_status = 'resolved'
                    price_status = 'pending'
                else:
                    info_status = 'pending'
                    price_status = 'pending'

                conn.execute(
                    """
                    INSERT INTO session_items
                        (session_id, barcode, delta, first_scanned_at, info_status, price_status)
                    VALUES (?, ?, 1, ?, ?, ?)
                    ON CONFLICT(session_id, barcode) DO UPDATE SET delta = delta + 1
                    """,
                    (session_id, body.barcode, now, info_status, price_status)
                )

                delta = conn.execute(
                    "SELECT delta FROM session_items WHERE session_id = ? AND barcode = ?",
                    (session_id, body.barcode)
                ).fetchone()['delta']

            return ScanResponse(barcode=body.barcode, session_delta=delta)
        finally:
            conn.close()
    except HTTPException:
        raise
    except sqlite3.OperationalError:
        raise _503
