import logging
import sqlite3
from datetime import datetime, UTC

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import get_db
from src.api.models import ScanRequest, ScanResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/scan", response_model=ScanResponse)
def scan(body: ScanRequest, db: sqlite3.Connection = Depends(get_db)):
    try:
        with db:
            row = db.execute("SELECT value FROM config WHERE key = 'scan_mode'").fetchone()
            if row is None:
                logger.warning("scan_mode missing from config, defaulting to 'out'")
                direction = "out"
            else:
                direction = row["value"]

            retailer = db.execute(
                "SELECT id FROM retailers WHERE name = ?", ("Sainsbury's",)
            ).fetchone()
            retailer_id = retailer["id"]

            db.execute(
                "INSERT INTO scan_events (barcode, retailer_id, direction, timestamp) VALUES (?, ?, ?, ?)",
                (body.barcode, retailer_id, direction, datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")),
            )

            barcode_row = db.execute(
                "SELECT product_variant_id FROM barcodes "
                "WHERE barcode = ? AND retailer_id = ? AND product_variant_id IS NOT NULL",
                (body.barcode, retailer_id),
            ).fetchone()

            known = barcode_row is not None
            if known:
                pvid = barcode_row["product_variant_id"]
                inv = db.execute(
                    "SELECT quantity FROM inventory WHERE product_variant_id = ?", (pvid,)
                ).fetchone()
                qty = inv["quantity"] if inv else 0
                if direction == "in":
                    new_qty = qty + 1
                else:
                    if qty == 0:
                        logger.warning("Scan out for %s but quantity already 0", body.barcode)
                    new_qty = max(0, qty - 1)
                db.execute(
                    "INSERT INTO inventory (product_variant_id, quantity, minimum_quantity) VALUES (?, ?, 0) "
                    "ON CONFLICT(product_variant_id) DO UPDATE SET quantity = excluded.quantity",
                    (pvid, new_qty),
                )
            else:
                db.execute(
                    "INSERT OR IGNORE INTO barcodes (barcode, retailer_id) VALUES (?, ?)",
                    (body.barcode, retailer_id),
                )
                db.execute(
                    "INSERT OR IGNORE INTO pending_lookups (barcode, retailer_id, status) VALUES (?, ?, 'pending')",
                    (body.barcode, retailer_id),
                )

        return ScanResponse(status="ok", direction=direction, known=known)
    except sqlite3.Error as e:
        logger.error("DB error during scan: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")
