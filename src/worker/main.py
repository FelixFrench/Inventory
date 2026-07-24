"""Async barcode-resolution worker. Polls session_items and resolves via OFF + Sainsbury's."""

import logging
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from src.db.db import get_connection
from src.worker import off
from src.worker import sainsburys

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECS = 5
OFF_RATE_LIMIT_SECS = 4

# Poll-3 (background retry/refresh) cadence. Failed OFF/price lookups are retried no more often than
# once per interval; resolved prices are refreshed for inflation on a slower cadence. LOOKUP_FAILURE_CAP
# stops a permanently-dead barcode/price from being retried forever.
OFF_RETRY_INTERVAL = timedelta(hours=24)
PRICE_RETRY_INTERVAL = timedelta(hours=24)
PRICE_REFRESH_INTERVAL = timedelta(days=30)
LOOKUP_FAILURE_CAP = 5


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

    # Manual refresh (3c) sits between live scanning and the background scheduler.
    if _poll_manual(db):
        return True

    return _poll3(db)


def _attempt_price(
    barcode: str,
    retailer_id: int,
    name: str | None,
    brand: str | None,
    product_quantity: str | None,
) -> None:
    """Session-less price attempt: query Sainsbury's and write the durable ``prices`` row only.

    Shared by poll-3's OFF-retry→price chaining (3.1a), price retry (3.1b), and price refresh (3.1c).
    Writes NO ``session_items`` stamp — the live poll-2 path owns that. Not paced (price calls never
    are). A null/empty ``name`` means no Sainsbury's keyword can be built, so the scraper is skipped
    entirely (mirrors the poll-2 null-name guard); the query builder would otherwise fire a
    ``"None None ..."`` search.
    """
    if not name:
        logger.info(f"Barcode {barcode}: no name to build a Sainsbury's keyword — price skipped")
        return

    try:
        price = sainsburys.get_price(
            barcode=barcode,
            name=name,
            brand=brand,
            product_quantity=product_quantity,
        )
    except Exception as e:
        logger.warning(f"Barcode {barcode}: Sainsbury's error — {e}")
        price = None

    conn = get_connection()
    try:
        now = datetime.now(UTC).isoformat(timespec='seconds')
        conn.execute("BEGIN IMMEDIATE")
        _upsert_price_durable(conn, barcode, retailer_id, price, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _write_off_retry(
    barcode: str,
    retailer_id: int,
    result: dict | None,
    clear_manual_marker: bool = False,
) -> None:
    """Write an OFF-retry outcome: durable ``product_variants`` row + OFF timestamp, one txn.

    No ``session_items`` stamp (this path has no session context). ``result`` is the OFF dict on
    success or ``None`` on failure — the caller has already collapsed a not-found / name-less response
    into ``None`` (§3.1a). Stamping ``worker_state.off_last_called_at`` here keeps ``_pace_off_call``
    correct on the next OFF call. Both timestamps derive from one ``now`` so they cannot straddle a
    second boundary.

    When ``clear_manual_marker`` is True (the 3c manual-refresh path), also clear
    ``product_variants.manual_refresh_requested`` for this variant IN THE SAME transaction as the
    durable write, so there is no window where the OFF write committed but the marker is still set
    (which would re-trigger the refresh and a second OFF call). Reused additively — the background
    3a/3b callers leave it False and are unaffected.
    """
    conn = get_connection()
    try:
        now_dt = datetime.now(UTC)
        conn.execute("BEGIN IMMEDIATE")
        _upsert_variant_durable(conn, barcode, retailer_id, result, now_dt.isoformat(timespec='seconds'))
        conn.execute(
            "UPDATE worker_state SET off_last_called_at = ? WHERE id = 1",
            (now_dt.isoformat(),),
        )
        if clear_manual_marker:
            conn.execute(
                "UPDATE product_variants SET manual_refresh_requested = 0 "
                "WHERE barcode = ? AND retailer_id = ?",
                (barcode, retailer_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _poll_manual(db: sqlite3.Connection) -> bool:
    """Manual-refresh path (Sprint 2, Phase 3c) — a user-triggered on-demand OFF + price refresh.

    Slotted BETWEEN poll 2 and poll 3: live scanning (poll 1/2) always preempts it, and it preempts
    the background scheduler (poll 3). Processes AT MOST ONE marked unit per call, then returns, so
    the loop re-checks live work on the next tick (same single-unit discipline as poll 1/2/3).

    A manual request always attempts: the 24h/30d staleness timers and the failure cap of 5 are
    deliberately IGNORED here (unlike poll 3), so a user can force a re-resolve of a ``pending``
    null-data row or retry a ``failed`` row on demand. Still goes through ``_pace_off_call`` and still
    stamps ``off_last_called_at`` (via ``_write_off_retry``) so cross-iteration OFF pacing holds.

    Never reads or writes ``session_items``. Returns True iff a unit was processed.
    """
    # Oldest-first by rowid for FIFO fairness (product_variants is a rowid table — composite PK, not
    # WITHOUT ROWID). Single-user app, so strict ordering barely matters.
    row = db.execute(
        "SELECT barcode, retailer_id FROM product_variants "
        "WHERE manual_refresh_requested = 1 ORDER BY rowid ASC LIMIT 1"
    ).fetchone()
    if not row:
        return False

    barcode = row['barcode']
    retailer_id = row['retailer_id']
    logger.info(f"Barcode {barcode}: manual refresh requested (retailer {retailer_id})")

    _pace_off_call(db)
    try:
        result = off.lookup_barcode(barcode)
    except Exception as e:
        logger.warning(f"Barcode {barcode}: manual refresh OFF network error — {e}")
        result = None

    # Collapse "not found" and "found but nameless" into a single failure value (mirrors poll 1 / 3a):
    # this one value drives both the durable write and the price-chain gate below.
    if result is None or result.get('name') is None:
        result = None

    if result is None:
        logger.info(f"Barcode {barcode}: manual refresh OFF failed (retailer {retailer_id}); marker cleared")
    else:
        logger.info(f"Barcode {barcode}: manual refresh OFF resolved (retailer {retailer_id}); marker cleared")

    # Clear the marker in the SAME transaction as the durable OFF write (success or failure).
    _write_off_retry(barcode, retailer_id, result, clear_manual_marker=True)

    # On OFF success, chain a fresh price lookup UNCONDITIONALLY — a manual refresh re-attempts the
    # price regardless of an existing prices row (differs from poll 3's chain-only-when-no-prices-row),
    # to give a genuine full refresh. Mirrors the live poll-1/poll-3 chain call site.
    if result is not None:
        logger.info(f"Barcode {barcode}: manual refresh chaining price attempt")
        _attempt_price(
            barcode, retailer_id,
            result['name'], result['brand'], result['product_quantity'],
        )
    return True


def _poll3(db: sqlite3.Connection) -> bool:
    """Poll 3 — the strictly-lowest-priority background retry/refresh path.

    Reached only when poll 1 and poll 2 both found no live work. Processes AT MOST ONE unit of work
    per call, in a fixed, testable category order — OFF retries, then price retries, then price
    refreshes; oldest-due first within a category — then returns so the loop re-checks live work on
    the next tick. Never reads or writes ``session_items`` / the confirm gate.

    Staleness boundaries are computed in Python and passed as bound parameters. All durable timestamps
    are UTC ``+00:00`` at 1-second resolution, so a lexical ``<=`` comparison against a UTC boundary is
    chronologically correct. A ``NULL`` ``last_lookup_datetime`` (backfilled row) is maximally stale:
    the ``IS NULL`` clause selects it, and ``ORDER BY last_lookup_datetime ASC`` sorts it first.
    """
    now = datetime.now(UTC)

    # (a) OFF retry — failed OFF rows only; pending/resolved are never selected here. Gated to
    # in-stock-or-minimum variants (3d) — see the stock predicate note below.
    off_boundary = (now - OFF_RETRY_INTERVAL).isoformat(timespec='seconds')
    off_row = db.execute(
        "SELECT pv.barcode, pv.retailer_id FROM product_variants pv "
        "LEFT JOIN inventory inv ON inv.barcode = pv.barcode AND inv.retailer_id = pv.retailer_id "
        "WHERE pv.lookup_status = 'failed' AND pv.lookup_failure_count < ? "
        "  AND (pv.last_lookup_datetime IS NULL OR pv.last_lookup_datetime <= ?) "
        "  AND (inv.quantity > 0 OR pv.minimum_quantity > 0) "
        "ORDER BY pv.last_lookup_datetime ASC LIMIT 1",
        (LOOKUP_FAILURE_CAP, off_boundary),
    ).fetchone()

    if off_row:
        barcode = off_row['barcode']
        retailer_id = off_row['retailer_id']

        _pace_off_call(db)
        try:
            result = off.lookup_barcode(barcode)
        except Exception as e:
            logger.warning(f"Barcode {barcode}: OFF retry network error — {e}")
            result = None

        # Collapse "not found" and "found but nameless" into a single failure value. This one value
        # drives BOTH the durable write and the chaining gate below — a name-less dict must never be
        # written as 'resolved' nor chain to a price attempt.
        if result is None or result.get('name') is None:
            result = None

        if result is None:
            logger.info(f"Barcode {barcode}: OFF retry failed (retailer {retailer_id})")
        else:
            logger.info(f"Barcode {barcode}: OFF retry resolved (retailer {retailer_id})")

        _write_off_retry(barcode, retailer_id, result)

        # OFF-retry → price chaining: only when OFF now resolved AND the price was never attempted
        # (no prices row at all). A prices row that exists but failed is owned by the price-retry path
        # (b), not re-attempted here.
        if result is not None:
            has_price = db.execute(
                "SELECT 1 FROM prices WHERE barcode = ? AND retailer_id = ?",
                (barcode, retailer_id),
            ).fetchone()
            if has_price is None:
                logger.info(f"Barcode {barcode}: chaining initial price attempt after OFF retry")
                _attempt_price(
                    barcode, retailer_id,
                    result['name'], result['brand'], result['product_quantity'],
                )
        return True

    # (b) Price retry — failed prices whose variant has a usable name. Gated to in-stock-or-minimum
    # variants (3d) — see the stock predicate note above.
    price_boundary = (now - PRICE_RETRY_INTERVAL).isoformat(timespec='seconds')
    retry_row = db.execute(
        "SELECT p.barcode, p.retailer_id, pv.name, pv.brand, pv.product_quantity "
        "FROM prices p "
        "JOIN product_variants pv ON pv.barcode = p.barcode AND pv.retailer_id = p.retailer_id "
        "LEFT JOIN inventory inv ON inv.barcode = pv.barcode AND inv.retailer_id = pv.retailer_id "
        "WHERE p.lookup_status = 'failed' AND p.lookup_failure_count < ? "
        "  AND (p.last_lookup_datetime IS NULL OR p.last_lookup_datetime <= ?) "
        "  AND pv.name IS NOT NULL "
        "  AND (inv.quantity > 0 OR pv.minimum_quantity > 0) "
        "ORDER BY p.last_lookup_datetime ASC LIMIT 1",
        (LOOKUP_FAILURE_CAP, price_boundary),
    ).fetchone()

    if retry_row:
        logger.info(f"Barcode {retry_row['barcode']}: price retry (retailer {retry_row['retailer_id']})")
        _attempt_price(
            retry_row['barcode'], retry_row['retailer_id'],
            retry_row['name'], retry_row['brand'], retry_row['product_quantity'],
        )
        return True

    # (c) Price refresh — resolved prices, for inflation. per_kg rows keep their stored £/kg (skipped).
    # Gated to in-stock-or-minimum variants (3d) — see the stock predicate note above.
    refresh_boundary = (now - PRICE_REFRESH_INTERVAL).isoformat(timespec='seconds')
    refresh_row = db.execute(
        "SELECT p.barcode, p.retailer_id, pv.name, pv.brand, pv.product_quantity "
        "FROM prices p "
        "JOIN product_variants pv ON pv.barcode = p.barcode AND pv.retailer_id = p.retailer_id "
        "LEFT JOIN inventory inv ON inv.barcode = pv.barcode AND inv.retailer_id = pv.retailer_id "
        "WHERE p.lookup_status = 'resolved' AND p.price_type != 'per_kg' "
        "  AND (p.last_lookup_datetime IS NULL OR p.last_lookup_datetime <= ?) "
        "  AND pv.name IS NOT NULL "
        "  AND (inv.quantity > 0 OR pv.minimum_quantity > 0) "
        "ORDER BY p.last_lookup_datetime ASC LIMIT 1",
        (refresh_boundary,),
    ).fetchone()

    if refresh_row:
        logger.info(f"Barcode {refresh_row['barcode']}: price refresh (retailer {refresh_row['retailer_id']})")
        _attempt_price(
            refresh_row['barcode'], refresh_row['retailer_id'],
            refresh_row['name'], refresh_row['brand'], refresh_row['product_quantity'],
        )
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
