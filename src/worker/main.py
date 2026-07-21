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


def _seconds_until_off_allowed(db: sqlite3.Connection) -> float:
    """Remaining OFF rate-limit gap (seconds) from the current worker_state timestamp."""
    row = db.execute("SELECT off_last_called_at FROM worker_state WHERE id = 1").fetchone()
    return _compute_startup_sleep(row['off_last_called_at'])


def _pace_off_call(db: sqlite3.Connection) -> None:
    """Sleep the remainder of the 4s OFF gap before an OFF call.

    The read and the sleep happen OUTSIDE any open DB transaction — the OFF call is already
    made before ``BEGIN IMMEDIATE`` (network calls never happen inside a transaction), so pacing
    immediately before it is likewise transaction-free. Single-OFF-caller invariant holds: one
    process, one loop, no concurrency to guard against.
    """
    sleep_secs = _seconds_until_off_allowed(db)
    if sleep_secs > 0:
        logger.info(f"Pacing OFF call: sleeping {sleep_secs:.1f}s to honour the {OFF_RATE_LIMIT_SECS}s gap")
        time.sleep(sleep_secs)


def _upsert_variant_durable(
    conn: sqlite3.Connection,
    barcode: str,
    retailer_id: int,
    result: dict | None,
    now: str,
) -> None:
    """Upsert the durable ``product_variants`` row. No ``session_items`` stamp — the caller owns
    the transaction and adds the stamp separately (3b's refresh path reuses this with no stamp).

    ``result`` is the OFF dict on success, ``None`` on failure. Success writes the OFF-owned
    columns + durable columns and resets ``lookup_failure_count`` to 0. Failure inserts a fresh
    null-data row (``lookup_status='failed'``, count 1); on conflict it touches ONLY the three
    durable columns — ``name``/``brand``/``product_quantity``/``minimum_quantity`` are left
    intact, so a failure never nulls previously-cached OFF data or resets a user-set minimum.
    """
    if result is not None:
        conn.execute(
            "INSERT INTO product_variants "
            "(barcode, retailer_id, name, brand, product_quantity, "
            " lookup_status, lookup_failure_count, last_lookup_datetime) "
            "VALUES (?, ?, ?, ?, ?, 'resolved', 0, ?) "
            "ON CONFLICT(barcode, retailer_id) DO UPDATE SET "
            "    name = excluded.name, "
            "    brand = excluded.brand, "
            "    product_quantity = excluded.product_quantity, "
            "    lookup_status = 'resolved', "
            "    lookup_failure_count = 0, "
            "    last_lookup_datetime = excluded.last_lookup_datetime",
            (barcode, retailer_id, result['name'], result['brand'], result['product_quantity'], now),
        )
    else:
        conn.execute(
            "INSERT INTO product_variants "
            "(barcode, retailer_id, name, brand, product_quantity, "
            " lookup_status, lookup_failure_count, last_lookup_datetime) "
            "VALUES (?, ?, NULL, NULL, NULL, 'failed', 1, ?) "
            "ON CONFLICT(barcode, retailer_id) DO UPDATE SET "
            "    lookup_status = 'failed', "
            "    lookup_failure_count = product_variants.lookup_failure_count + 1, "
            "    last_lookup_datetime = excluded.last_lookup_datetime",
            (barcode, retailer_id, now),
        )


def _upsert_price_durable(
    conn: sqlite3.Connection,
    barcode: str,
    retailer_id: int,
    price: dict | None,
    now: str,
) -> None:
    """Upsert the durable ``prices`` row. No ``session_items`` stamp — caller owns the txn.

    ``price`` is the Sainsbury's dict on success, ``None`` on failure. Success writes the price
    data + durable columns and resets the failure count. Failure inserts a fresh row with null
    price data (``price_type`` takes its ``'unit'`` default) and ``lookup_status='failed'``; on
    conflict it keeps the existing ``price_pence``/``price_type``/``product_url`` and touches
    only the durable columns.
    """
    if price is not None:
        conn.execute(
            "INSERT INTO prices "
            "(barcode, retailer_id, price_pence, price_type, product_url, "
            " lookup_status, lookup_failure_count, last_lookup_datetime) "
            "VALUES (?, ?, ?, ?, ?, 'resolved', 0, ?) "
            "ON CONFLICT(barcode, retailer_id) DO UPDATE SET "
            "    price_pence = excluded.price_pence, "
            "    price_type = excluded.price_type, "
            "    product_url = excluded.product_url, "
            "    lookup_status = 'resolved', "
            "    lookup_failure_count = 0, "
            "    last_lookup_datetime = excluded.last_lookup_datetime",
            (barcode, retailer_id, price['price_pence'], price['price_type'], price.get('product_url'), now),
        )
    else:
        conn.execute(
            "INSERT INTO prices "
            "(barcode, retailer_id, lookup_status, lookup_failure_count, last_lookup_datetime) "
            "VALUES (?, ?, 'failed', 1, ?) "
            "ON CONFLICT(barcode, retailer_id) DO UPDATE SET "
            "    lookup_status = 'failed', "
            "    lookup_failure_count = prices.lookup_failure_count + 1, "
            "    last_lookup_datetime = excluded.last_lookup_datetime",
            (barcode, retailer_id, now),
        )


def _phase1_success(barcode: str, session_id: int, retailer_id: int, result: dict) -> int:
    """Write OFF success: durable variant row + info_status stamp + OFF timestamp, one txn.

    Returns the rowcount of the session_items UPDATE.
    """
    conn = get_connection()
    try:
        now_dt = datetime.now(UTC)
        conn.execute("BEGIN IMMEDIATE")
        _upsert_variant_durable(conn, barcode, retailer_id, result, now_dt.isoformat(timespec='seconds'))
        r = conn.execute(
            "UPDATE session_items SET info_status = 'resolved' "
            "WHERE session_id = ? AND barcode = ? AND retailer_id = ?",
            (session_id, barcode, retailer_id),
        )
        conn.execute(
            "UPDATE worker_state SET off_last_called_at = ? WHERE id = 1",
            (now_dt.isoformat(),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return r.rowcount


def _phase1_failure(barcode: str, session_id: int, retailer_id: int) -> int:
    """Write OFF failure: durable null-data variant row (lookup_status='failed') + info/price
    stamp + OFF timestamp, one txn. Writes no prices row (price never attempted).

    Returns the rowcount of the session_items UPDATE.
    """
    conn = get_connection()
    try:
        now_dt = datetime.now(UTC)
        conn.execute("BEGIN IMMEDIATE")
        _upsert_variant_durable(conn, barcode, retailer_id, None, now_dt.isoformat(timespec='seconds'))
        r = conn.execute(
            "UPDATE session_items SET info_status = 'failed', price_status = 'not_possible' "
            "WHERE session_id = ? AND barcode = ? AND retailer_id = ?",
            (session_id, barcode, retailer_id),
        )
        conn.execute(
            "UPDATE worker_state SET off_last_called_at = ? WHERE id = 1",
            (now_dt.isoformat(),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return r.rowcount


def _phase2_success(barcode: str, session_id: int, retailer_id: int, price: dict) -> int:
    """Write price success: durable price row + price_status stamp, one txn.

    Returns the rowcount of the session_items UPDATE.
    """
    conn = get_connection()
    try:
        now_dt = datetime.now(UTC)
        conn.execute("BEGIN IMMEDIATE")
        _upsert_price_durable(conn, barcode, retailer_id, price, now_dt.isoformat(timespec='seconds'))
        r = conn.execute(
            "UPDATE session_items SET price_status = 'resolved' "
            "WHERE session_id = ? AND barcode = ? AND retailer_id = ?",
            (session_id, barcode, retailer_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return r.rowcount


def _phase2_failure(barcode: str, session_id: int, retailer_id: int) -> int:
    """Write price failure: durable price row (lookup_status='failed', existing price data kept)
    + price_status stamp, one txn.

    Returns the rowcount of the session_items UPDATE.
    """
    conn = get_connection()
    try:
        now_dt = datetime.now(UTC)
        conn.execute("BEGIN IMMEDIATE")
        _upsert_price_durable(conn, barcode, retailer_id, None, now_dt.isoformat(timespec='seconds'))
        r = conn.execute(
            "UPDATE session_items SET price_status = 'failed' "
            "WHERE session_id = ? AND barcode = ? AND retailer_id = ?",
            (session_id, barcode, retailer_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return r.rowcount


def _phase2_not_possible(barcode: str, session_id: int, retailer_id: int) -> int:
    """Stamp price_status='not_possible' for an item with no name to build a keyword from.

    Writes NO prices row (price was never attempted) — "price never attempted" is encoded by the
    absence of a prices row. Mirrors _phase1_failure's "no name ⇒ not_possible" semantics.
    Returns the rowcount of the session_items UPDATE.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        r = conn.execute(
            "UPDATE session_items SET price_status = 'not_possible' "
            "WHERE session_id = ? AND barcode = ? AND retailer_id = ?",
            (session_id, barcode, retailer_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return r.rowcount


def _poll_iteration(db: sqlite3.Connection) -> bool:
    """Process at most one pending item.

    Returns True if an item was attempted (caller should poll again immediately with no idle
    sleep), False if there was nothing to do (caller should sleep).

    The ``retailer_id`` for the whole tick comes from the poll SELECT row (the composite key
    ``session_items`` carries), and is threaded consistently into the durable
    ``product_variants`` / ``prices`` writes and the ``session_items`` stamps.

    On the zero-rowcount paths below: when a write-back's ``session_items`` UPDATE affects zero
    rows, the session was discarded mid-lookup. The ``product_variants`` / ``prices`` rows written
    by the durable writers are deliberately retained — those tables are a persistent cache keyed
    by ``(barcode, retailer_id)``, so the fetched data warms the cache for a later rescan. The
    durable-row write and the stamp share one transaction that commits unconditionally, so a
    zero-rowcount stamp still leaves the durable row committed; it is a logged signal, not an
    error or a rollback trigger.
    """
    # Poll 1: info pending
    poll1 = db.execute(
        "SELECT barcode, session_id, retailer_id FROM session_items "
        "WHERE info_status = 'pending' "
        "ORDER BY first_scanned_at ASC LIMIT 1"
    ).fetchone()

    if poll1:
        barcode = poll1['barcode']
        session_id = poll1['session_id']
        retailer_id = poll1['retailer_id']

        _pace_off_call(db)
        try:
            result = off.lookup_barcode(barcode)
        except Exception as e:
            logger.warning(f"Barcode {barcode}: OFF network error — {e}")
            rowcount = _phase1_failure(barcode, session_id, retailer_id)
            if rowcount == 0:
                logger.info(f"Session {session_id} discarded mid-resolution for {barcode}; variant row kept")
            return True

        if result is None or result.get('name') is None:
            logger.info(f"Barcode {barcode}: not found on OFF or no product name")
            rowcount = _phase1_failure(barcode, session_id, retailer_id)
            if rowcount == 0:
                logger.info(f"Session {session_id} discarded mid-resolution for {barcode}; variant row kept")
            return True

        rowcount = _phase1_success(barcode, session_id, retailer_id, result)
        if rowcount == 0:
            logger.info(f"Session {session_id} discarded mid-resolution for {barcode}; dropping result")
            return True

        try:
            price = sainsburys.get_price(
                barcode=barcode,
                name=result['name'],
                brand=result['brand'],
                product_quantity=result['product_quantity'],
            )
        except Exception as e:
            logger.warning(f"Barcode {barcode}: Sainsbury's error — {e}")
            price = None

        if price is not None:
            rowcount2 = _phase2_success(barcode, session_id, retailer_id, price)
        else:
            rowcount2 = _phase2_failure(barcode, session_id, retailer_id)

        if rowcount2 == 0:
            logger.info(f"Session {session_id} discarded mid-resolution for {barcode}; prices row kept")
        return True

    # Poll 2: price pending (only when Poll 1 found nothing)
    poll2 = db.execute(
        """
        SELECT si.barcode, si.session_id, si.retailer_id, pv.name, pv.brand, pv.product_quantity
        FROM   session_items si
        LEFT   JOIN product_variants pv
               ON pv.barcode = si.barcode AND pv.retailer_id = si.retailer_id
        WHERE  si.info_status = 'resolved'
          AND  si.price_status = 'pending'
        ORDER  BY si.first_scanned_at ASC
        LIMIT  1
        """
    ).fetchone()

    if poll2:
        barcode = poll2['barcode']
        session_id = poll2['session_id']
        retailer_id = poll2['retailer_id']

        # A null/empty name means no Sainsbury's keyword can be built (Sainsbury's can't be
        # queried by barcode). This is reachable when a failed-OFF barcode — now carrying a
        # null-data product_variants row (always-write-row, 3a) — is re-scanned: scan.py sees the
        # row exists and stamps info_status='resolved', routing the item here. Skip the scraper
        # entirely, write no prices row, and stamp price_status='not_possible' (terminal) so the
        # item is not re-selected every tick and no garbage query is fired. Mirrors
        # _phase1_failure's "no name ⇒ not_possible". Poll-1's price chain runs only after
        # _phase1_success (non-null name guaranteed), so this guard is only needed here.
        if not poll2['name']:
            logger.info(f"Barcode {barcode}: no name to build a Sainsbury's keyword — price not possible")
            rowcount = _phase2_not_possible(barcode, session_id, retailer_id)
            if rowcount == 0:
                logger.info(f"Session {session_id} discarded mid-resolution for {barcode}")
            return True

        try:
            price = sainsburys.get_price(
                barcode=barcode,
                name=poll2['name'],
                brand=poll2['brand'],
                product_quantity=poll2['product_quantity'],
            )
        except Exception as e:
            logger.warning(f"Barcode {barcode}: Sainsbury's error — {e}")
            price = None

        if price is not None:
            rowcount = _phase2_success(barcode, session_id, retailer_id, price)
        else:
            rowcount = _phase2_failure(barcode, session_id, retailer_id)

        if rowcount == 0:
            logger.info(f"Session {session_id} discarded mid-resolution for {barcode}")
        return True

    return False


def _poll_forever(db: sqlite3.Connection) -> None:
    """Run the poll loop forever.

    An unexpected exception in a single iteration (e.g. sqlite3.OperationalError under WAL write
    contention) is logged and the loop continues, rather than propagating out and killing the
    always-on worker process. Mirrors the guard in the FastAPI ``_session_poll_loop`` background
    task.
    """
    while True:
        try:
            did_work = _poll_iteration(db)
        except Exception:
            logger.exception("Worker poll iteration failed")
            did_work = False
        if not did_work:
            time.sleep(POLL_INTERVAL_SECS)


def main() -> None:
    load_dotenv(Path(__file__).parents[2] / "config.local.env")
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )

    conn = get_connection()
    try:
        # Fail-fast validation that the Sainsbury's retailer exists. The per-tick retailer_id now
        # comes from each poll SELECT row (§2.4), not from this lookup.
        retailer_row = conn.execute(
            "SELECT id FROM retailers WHERE name = 'Sainsbury''s'"
        ).fetchone()
        if retailer_row is None:
            logger.error("Sainsbury's retailer not found in DB — worker cannot start")
            sys.exit(1)

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
    _poll_forever(db)


if __name__ == "__main__":
    main()
