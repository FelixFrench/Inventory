"""Async barcode-resolution worker. Polls session_items and resolves via OFF + Sainsbury's."""

import logging
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from src.db.db import get_connection
from src.worker import off
from src.worker import sainsburys

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECS = 5
OFF_RATE_LIMIT_SECS = 4


def _compute_startup_sleep(off_last_called_at: str) -> float:
    last_called = datetime.fromisoformat(off_last_called_at)
    if last_called.tzinfo is None:
        last_called = last_called.replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - last_called).total_seconds()
    return max(0.0, float(OFF_RATE_LIMIT_SECS) - elapsed)


def _phase1_success(barcode: str, session_id: int, retailer_id: int, result: dict) -> int:
    """Write OFF success result. Returns rowcount of session_items UPDATE."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR REPLACE INTO product_variants (barcode, retailer_id, name, brand, weight_g) "
            "VALUES (?, ?, ?, ?, ?)",
            (barcode, retailer_id, result['name'], result['brand'], result['weight_g'])
        )
        r = conn.execute(
            "UPDATE session_items SET info_status = 'resolved' "
            "WHERE session_id = ? AND barcode = ?",
            (session_id, barcode)
        )
        conn.execute(
            "UPDATE worker_state SET off_last_called_at = ? WHERE id = 1",
            (datetime.now(UTC).isoformat(),)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return r.rowcount


def _phase1_failure(barcode: str, session_id: int) -> int:
    """Write OFF failure result. Returns rowcount of session_items UPDATE."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        r = conn.execute(
            "UPDATE session_items SET info_status = 'failed', price_status = 'not_possible' "
            "WHERE session_id = ? AND barcode = ?",
            (session_id, barcode)
        )
        conn.execute(
            "UPDATE worker_state SET off_last_called_at = ? WHERE id = 1",
            (datetime.now(UTC).isoformat(),)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return r.rowcount


def _phase2_success(barcode: str, session_id: int, retailer_id: int, price: dict) -> int:
    """Write Sainsbury's success result. Returns rowcount of session_items UPDATE."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR REPLACE INTO prices (barcode, retailer_id, price_pence, price_type, product_url) "
            "VALUES (?, ?, ?, ?, ?)",
            (barcode, retailer_id, price['price_pence'], price['price_type'], price.get('product_url'))
        )
        r = conn.execute(
            "UPDATE session_items SET price_status = 'resolved' "
            "WHERE session_id = ? AND barcode = ?",
            (session_id, barcode)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return r.rowcount


def _phase2_failure(barcode: str, session_id: int) -> int:
    """Write Sainsbury's failure result. Returns rowcount of session_items UPDATE."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        r = conn.execute(
            "UPDATE session_items SET price_status = 'failed' "
            "WHERE session_id = ? AND barcode = ?",
            (session_id, barcode)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return r.rowcount


def main() -> None:
    load_dotenv(Path(__file__).parents[2] / "config.local.env")
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )

    conn = get_connection()
    try:
        retailer_row = conn.execute(
            "SELECT id FROM retailers WHERE name = 'Sainsbury''s'"
        ).fetchone()
        if retailer_row is None:
            logger.error("Sainsbury's retailer not found in DB — worker cannot start")
            sys.exit(1)
        retailer_id = retailer_row['id']

        state_row = conn.execute(
            "SELECT off_last_called_at FROM worker_state WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    sleep_secs = _compute_startup_sleep(state_row['off_last_called_at'])
    if sleep_secs > 0:
        logger.info(f"Startup: sleeping {sleep_secs:.1f}s to honour OFF rate limit")
        time.sleep(sleep_secs)

    logger.info("Worker started")

    db = get_connection()
    while True:
        # Poll 1: info pending
        poll1 = db.execute(
            "SELECT barcode, session_id FROM session_items "
            "WHERE info_status = 'pending' "
            "ORDER BY first_scanned_at ASC LIMIT 1"
        ).fetchone()

        if poll1:
            barcode = poll1['barcode']
            session_id = poll1['session_id']

            try:
                result = off.lookup_barcode(barcode)
            except Exception as e:
                logger.warning(f"Barcode {barcode}: OFF network error — {e}")
                rowcount = _phase1_failure(barcode, session_id)
                if rowcount == 0:
                    logger.info(f"Session {session_id} discarded mid-resolution for {barcode}")
                continue

            if result is None or result.get('name') is None:
                logger.info(f"Barcode {barcode}: not found on OFF or no product name")
                rowcount = _phase1_failure(barcode, session_id)
                if rowcount == 0:
                    logger.info(f"Session {session_id} discarded mid-resolution for {barcode}")
                continue

            rowcount = _phase1_success(barcode, session_id, retailer_id, result)
            if rowcount == 0:
                logger.info(f"Session {session_id} discarded mid-resolution for {barcode}; dropping result")
                continue

            try:
                price = sainsburys.get_price(
                    barcode=barcode,
                    name=result['name'],
                    brand=result['brand'],
                    weight_g=result['weight_g'],
                )
            except Exception as e:
                logger.warning(f"Barcode {barcode}: Sainsbury's error — {e}")
                price = None

            if price is not None:
                rowcount2 = _phase2_success(barcode, session_id, retailer_id, price)
            else:
                rowcount2 = _phase2_failure(barcode, session_id)

            if rowcount2 == 0:
                logger.info(f"Session {session_id} discarded mid-resolution for {barcode}; prices row kept")
            continue

        # Poll 2: price pending (only when Poll 1 found nothing)
        poll2 = db.execute(
            """
            SELECT si.barcode, si.session_id, pv.name, pv.brand, pv.weight_g
            FROM   session_items si
            LEFT   JOIN product_variants pv ON pv.barcode = si.barcode AND pv.retailer_id = ?
            WHERE  si.info_status = 'resolved'
              AND  si.price_status = 'pending'
            ORDER  BY si.first_scanned_at ASC
            LIMIT  1
            """,
            (retailer_id,)
        ).fetchone()

        if poll2:
            barcode = poll2['barcode']
            session_id = poll2['session_id']

            try:
                price = sainsburys.get_price(
                    barcode=barcode,
                    name=poll2['name'],
                    brand=poll2['brand'],
                    weight_g=poll2['weight_g'],
                )
            except Exception as e:
                logger.warning(f"Barcode {barcode}: Sainsbury's error — {e}")
                price = None

            if price is not None:
                rowcount = _phase2_success(barcode, session_id, retailer_id, price)
            else:
                rowcount = _phase2_failure(barcode, session_id)

            if rowcount == 0:
                logger.info(f"Session {session_id} discarded mid-resolution for {barcode}")
            continue

        time.sleep(POLL_INTERVAL_SECS)


if __name__ == "__main__":
    main()
