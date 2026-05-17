"""Async barcode-resolution worker. Polls pending_lookups and resolves via OFF + Sainsbury's."""

import logging
import sqlite3
import sys
import time
from datetime import UTC, datetime

import requests
from dotenv import load_dotenv

from src.db.db import get_connection
from src.worker import off
from src.worker import sainsburys

log = logging.getLogger("worker")


def compute_startup_sleep(last_done_queued_at: datetime | None, now: datetime) -> float:
    if last_done_queued_at is None:
        return 0.0
    elapsed = (now - last_done_queued_at).total_seconds()
    return max(0.0, 4.0 - elapsed)


def _mark_failed(db: sqlite3.Connection, barcode: str, retailer_id: int) -> None:
    try:
        db.execute(
            "UPDATE pending_lookups SET status='failed' WHERE barcode=? AND retailer_id=?",
            (barcode, retailer_id),
        )
        db.commit()
    except sqlite3.Error as e:
        log.error(f"Barcode {barcode}: could not mark failed — {e}")


def process_row(
    barcode: str,
    retailer_id: int,
    queued_at: str,
    db: sqlite3.Connection | None = None,
) -> None:
    if db is None:
        db = get_connection()

    # Step 1 — OpenFoodFacts lookup
    try:
        result = off.lookup_barcode(barcode)
    except requests.RequestException as e:
        log.warning(f"Barcode {barcode}: network error — {e}")
        _mark_failed(db, barcode, retailer_id)
        return

    if result is None:
        log.info(f"Barcode {barcode}: not found on OpenFoodFacts")
        _mark_failed(db, barcode, retailer_id)
        return

    if result["name"] is None:
        log.info(f"Barcode {barcode}: no product name — cannot query Sainsbury's")
        _mark_failed(db, barcode, retailer_id)
        return

    # Step 5 — Sainsbury's price lookup (before DB transaction so exceptions don't abort it)
    try:
        price = sainsburys.get_price(
            barcode=barcode,
            name=result["name"],
            brand=result["brand"],
            weight_g=result["weight_g"],
        )
    except Exception as e:
        log.warning(f"Barcode {barcode}: Sainsbury's error — {e}")
        price = None

    # Steps 2–6 — all DB writes in a single transaction
    now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db:
            # 2. Insert product_variants
            cursor = db.execute(
                "INSERT INTO product_variants (name, brand, weight_g, info_source, info_last_updated)"
                " VALUES (?, ?, ?, 'openfoodfacts', ?)",
                (result["name"], result["brand"], result["weight_g"], now_str),
            )
            pv_id = cursor.lastrowid

            # 3. Update barcodes
            db.execute(
                "UPDATE barcodes SET product_variant_id=? WHERE barcode=? AND retailer_id=?",
                (pv_id, barcode, retailer_id),
            )

            # 4. Apply pending scan delta to inventory
            rows = db.execute(
                "SELECT direction, COUNT(*) as count FROM scan_events"
                " WHERE barcode=? AND retailer_id=? AND timestamp>=? GROUP BY direction",
                (barcode, retailer_id, queued_at),
            ).fetchall()
            count_in = sum(r["count"] for r in rows if r["direction"] == "in")
            count_out = sum(r["count"] for r in rows if r["direction"] == "out")
            quantity = max(0, count_in - count_out)
            n_events = count_in + count_out
            db.execute(
                "INSERT INTO inventory (product_variant_id, quantity, minimum_quantity) VALUES (?, ?, 0)",
                (pv_id, quantity),
            )
            log.info(
                f"Barcode {barcode}: inventory initialised at {quantity}"
                f" (delta from {n_events} scan events)"
            )

            # 5. Insert prices row
            if price is not None:
                db.execute(
                    "INSERT INTO prices (product_variant_id, retailer_id, price_pence, price_type, last_updated)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (pv_id, retailer_id, price["price_pence"], price["price_type"], now_str),
                )
            else:
                db.execute(
                    "INSERT INTO prices (product_variant_id, retailer_id, price_pence, price_type, last_updated)"
                    " VALUES (?, ?, NULL, 'unit', ?)",
                    (pv_id, retailer_id, now_str),
                )

            # 6. Mark done
            db.execute(
                "UPDATE pending_lookups SET status='done' WHERE barcode=? AND retailer_id=?",
                (barcode, retailer_id),
            )

        price_pence = price["price_pence"] if price else None
        log.info(f"Barcode {barcode}: lookup complete — name={result['name']}, price={price_pence}p")

    except sqlite3.Error as e:
        log.error(f"Barcode {barcode}: database error — {e}", exc_info=True)
        try:
            db.execute(
                "UPDATE pending_lookups SET status='failed' WHERE barcode=? AND retailer_id=?",
                (barcode, retailer_id),
            )
            db.commit()
        except sqlite3.Error as e2:
            log.error(f"Barcode {barcode}: could not mark failed after DB error — {e2}")


def main() -> None:
    load_dotenv("config.local.env")
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )

    db = get_connection()

    row = db.execute("SELECT id FROM retailers WHERE name=?", ("Sainsbury's",)).fetchone()
    retailer_id: int = row["id"]

    # Restart recovery: if we just processed a row, honour the 4s inter-request gap
    last_done = db.execute(
        "SELECT queued_at FROM pending_lookups WHERE status='done' ORDER BY queued_at DESC LIMIT 1"
    ).fetchone()
    last_done_dt: datetime | None = None
    if last_done:
        last_done_dt = datetime.fromisoformat(last_done["queued_at"])
        if last_done_dt.tzinfo is None:
            last_done_dt = last_done_dt.replace(tzinfo=UTC)
    sleep_secs = compute_startup_sleep(last_done_dt, datetime.now(UTC))
    if sleep_secs > 0:
        log.info(f"Startup recovery: sleeping {sleep_secs:.1f}s to honour OFF rate limit")
        time.sleep(sleep_secs)

    log.info("Worker started")
    while True:
        pending = db.execute(
            "SELECT barcode, retailer_id, queued_at FROM pending_lookups"
            " WHERE status='pending' ORDER BY queued_at ASC LIMIT 1"
        ).fetchone()

        if pending is None:
            time.sleep(5)
            continue

        process_row(
            barcode=pending["barcode"],
            retailer_id=pending["retailer_id"],
            queued_at=pending["queued_at"],
            db=db,
        )
        time.sleep(4)


if __name__ == "__main__":
    main()
