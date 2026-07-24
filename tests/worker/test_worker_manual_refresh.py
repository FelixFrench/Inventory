"""Tests for the manual-refresh worker path (_poll_manual) in src/worker/main.py — Sprint 2, Phase 3c.

_poll_manual is slotted between the live-scan polls (poll 1/2) and the background scheduler (poll 3).
A marked product_variants row (manual_refresh_requested = 1) always attempts an OFF call — the 24h/30d
timers and the failure cap are ignored — goes through pacing, writes the durable row, clears the marker
atomically, and on OFF success chains a price lookup unconditionally. Clock and network clients are
mocked for determinism, exactly as in test_worker_poll3.py.
"""

import sqlite3
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from src.worker.main import _poll_iteration, _poll_manual

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (
    barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    name TEXT, brand TEXT, product_quantity TEXT, minimum_quantity INTEGER NOT NULL DEFAULT 0,
    lookup_status TEXT NOT NULL DEFAULT 'pending' CHECK(lookup_status IN ('pending', 'resolved', 'failed')),
    lookup_failure_count INTEGER NOT NULL DEFAULT 0 CHECK(lookup_failure_count >= 0),
    last_lookup_datetime TEXT,
    manual_refresh_requested INTEGER NOT NULL DEFAULT 0 CHECK(manual_refresh_requested IN (0, 1)),
    PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (
    barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER,
    price_type TEXT NOT NULL DEFAULT 'unit' CHECK(price_type IN ('unit', 'per_kg')), product_url TEXT NULL,
    lookup_status TEXT NOT NULL DEFAULT 'pending' CHECK(lookup_status IN ('pending', 'resolved', 'failed')),
    lookup_failure_count INTEGER NOT NULL DEFAULT 0 CHECK(lookup_failure_count >= 0),
    last_lookup_datetime TEXT,
    PRIMARY KEY (barcode, retailer_id),
    FOREIGN KEY (barcode, retailer_id) REFERENCES product_variants(barcode, retailer_id));
CREATE TABLE sessions (id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('in', 'out')), started_at TEXT NOT NULL, recovered_at TEXT);
CREATE TABLE session_items (session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL, delta INTEGER NOT NULL CHECK(delta >= 0), info_status TEXT NOT NULL DEFAULT 'pending' CHECK(info_status IN ('pending', 'resolved', 'failed')), price_status TEXT NOT NULL DEFAULT 'pending' CHECK(price_status IN ('pending', 'resolved', 'failed', 'not_possible')), first_scanned_at TEXT NOT NULL, PRIMARY KEY (session_id, barcode, retailer_id));
CREATE INDEX idx_session_items_info_pending ON session_items(first_scanned_at) WHERE info_status = 'pending';
CREATE INDEX idx_session_items_price_pending ON session_items(first_scanned_at) WHERE price_status = 'pending';
CREATE TABLE inventory (
    barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    quantity INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (barcode, retailer_id));
CREATE TABLE worker_state (id INTEGER PRIMARY KEY CHECK(id = 1), off_last_called_at TEXT NOT NULL);
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO worker_state (id, off_last_called_at) VALUES (1, '1970-01-01T00:00:00');
"""

_BARCODE = "5014788110140"
_BARCODE2 = "5000000000000"
_RETAILER_ID = 1
_SESSION_ID = 1
_GOOD_OFF = {"name": "Baked Beans", "brand": "Heinz", "product_quantity": "415g"}
_GOOD_PRICE = {"price_pence": 85, "price_type": "unit", "product_url": "https://x/p"}

_NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec='seconds')


_RECENT = _iso(_NOW - timedelta(hours=1))   # well within 24h — poll 3 would NOT be due


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


class _MockConn:
    """Wraps a sqlite3.Connection but ignores close() to protect the test fixture."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        pass


def _make_get_connection(conn: sqlite3.Connection):
    return lambda: _MockConn(conn)


def _seed_variant(conn, *, barcode=_BARCODE, retailer_id=_RETAILER_ID, name=None, brand=None,
                  product_quantity=None, minimum_quantity=0, lookup_status='pending',
                  lookup_failure_count=0, last_lookup_datetime=None, manual_refresh_requested=0):
    conn.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (barcode,))
    conn.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name, brand, product_quantity, "
        "minimum_quantity, lookup_status, lookup_failure_count, last_lookup_datetime, "
        "manual_refresh_requested) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (barcode, retailer_id, name, brand, product_quantity, minimum_quantity,
         lookup_status, lookup_failure_count, last_lookup_datetime, manual_refresh_requested),
    )
    conn.commit()


def _seed_price(conn, *, barcode=_BARCODE, retailer_id=_RETAILER_ID, price_pence=None,
                price_type='unit', product_url=None, lookup_status='resolved',
                lookup_failure_count=0, last_lookup_datetime=None):
    conn.execute(
        "INSERT INTO prices (barcode, retailer_id, price_pence, price_type, product_url, "
        "lookup_status, lookup_failure_count, last_lookup_datetime) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (barcode, retailer_id, price_pence, price_type, product_url, lookup_status,
         lookup_failure_count, last_lookup_datetime),
    )
    conn.commit()


def _seed_session_item(conn, *, barcode=_BARCODE, retailer_id=_RETAILER_ID, session_id=_SESSION_ID,
                       info_status='pending', price_status='pending'):
    conn.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (barcode,))
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, type, started_at) VALUES (?, 'in', '2026-05-27T10:00:00')",
        (session_id,),
    )
    conn.execute(
        "INSERT INTO session_items (session_id, barcode, retailer_id, delta, info_status, "
        "price_status, first_scanned_at) VALUES (?, ?, ?, 1, ?, ?, '2026-05-27T10:00:00')",
        (session_id, barcode, retailer_id, info_status, price_status),
    )
    conn.commit()


class _Mocks:
    off = None
    price = None
    pace = None


@contextmanager
def _env(db, *, off_result=None, price_result=None, now=_NOW):
    """Patch the module's clock, connection factory, pacing, and network clients."""
    with ExitStack() as stack:
        stack.enter_context(patch("src.worker.main.get_connection", _make_get_connection(db)))
        mock_dt = stack.enter_context(patch("src.worker.main.datetime"))
        mock_dt.now.return_value = now
        mock_dt.fromisoformat = datetime.fromisoformat
        stack.enter_context(patch("src.worker.main.time.sleep"))
        m = _Mocks()
        m.off = stack.enter_context(patch("src.worker.off.lookup_barcode", return_value=off_result))
        m.price = stack.enter_context(patch("src.worker.sainsburys.get_price", return_value=price_result))
        m.pace = stack.enter_context(patch("src.worker.main._pace_off_call"))
        yield m


def _variant(db, barcode=_BARCODE):
    return db.execute(
        "SELECT * FROM product_variants WHERE barcode = ? AND retailer_id = ?", (barcode, _RETAILER_ID)
    ).fetchone()


# ---------------------------------------------------------------------------
# OFF success -> durable write + unconditional price chain + marker cleared
# ---------------------------------------------------------------------------

def test_manual_off_success_writes_durable_and_chains_price(db):
    """OFF resolves: durable variant row written via _write_off_retry (resolved, count reset 0,
    timestamp), price chained unconditionally, marker cleared."""
    _seed_variant(db, lookup_status='pending', manual_refresh_requested=1)
    with _env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE) as m:
        assert _poll_manual(db) is True

    pv = _variant(db)
    assert pv["lookup_status"] == "resolved"
    assert pv["name"] == "Baked Beans"
    assert pv["lookup_failure_count"] == 0
    assert pv["last_lookup_datetime"] == _iso(_NOW)
    assert pv["manual_refresh_requested"] == 0

    m.price.assert_called_once()
    assert m.price.call_args.kwargs["name"] == "Baked Beans"
    pr = db.execute(
        "SELECT price_pence, lookup_status FROM prices WHERE barcode = ? AND retailer_id = ?",
        (_BARCODE, _RETAILER_ID),
    ).fetchone()
    assert pr["price_pence"] == 85
    assert pr["lookup_status"] == "resolved"


def test_manual_off_success_chains_price_even_when_prices_row_exists(db):
    """Unlike poll 3, a manual refresh re-attempts the price even when a prices row already exists."""
    _seed_variant(db, lookup_status='resolved', name='Old', manual_refresh_requested=1)
    _seed_price(db, price_pence=50, lookup_status='resolved', last_lookup_datetime=_RECENT)
    with _env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE) as m:
        assert _poll_manual(db) is True

    m.price.assert_called_once()
    pr = db.execute(
        "SELECT price_pence FROM prices WHERE barcode = ? AND retailer_id = ?", (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pr["price_pence"] == 85  # re-scraped, not the stale 50


# ---------------------------------------------------------------------------
# OFF failure -> preserve cached data, no price, marker cleared
# ---------------------------------------------------------------------------

def test_manual_off_failure_preserves_and_skips_price(db):
    """OFF fails: variant flips to failed (count++), existing name/brand/quantity preserved, no
    price attempt, marker cleared."""
    _seed_variant(db, name='Baked Beans', brand='Heinz', product_quantity='415g',
                  lookup_status='resolved', lookup_failure_count=0,
                  last_lookup_datetime=_RECENT, manual_refresh_requested=1)
    with _env(db, off_result=None) as m:
        assert _poll_manual(db) is True

    pv = _variant(db)
    assert pv["lookup_status"] == "failed"
    assert pv["lookup_failure_count"] == 1
    assert pv["name"] == "Baked Beans"      # preserved, not nulled
    assert pv["brand"] == "Heinz"
    assert pv["product_quantity"] == "415g"
    assert pv["manual_refresh_requested"] == 0
    m.price.assert_not_called()


def test_manual_off_nameless_result_collapses_to_failure(db):
    """A truthy-but-nameless OFF response is treated as failure: no 'resolved', no price chain."""
    _seed_variant(db, lookup_status='pending', manual_refresh_requested=1)
    with _env(db, off_result={"name": None, "brand": None, "product_quantity": None}) as m:
        assert _poll_manual(db) is True

    assert _variant(db)["lookup_status"] == "failed"
    m.price.assert_not_called()


# ---------------------------------------------------------------------------
# Timer / cap bypass
# ---------------------------------------------------------------------------

def test_manual_ignores_timers_and_failure_cap(db):
    """A marked row attempts even when last_lookup_datetime is recent (<24h) and the failure count
    is at/over the poll-3 cap of 5 — the manual path has no staleness/cap gate."""
    _seed_variant(db, lookup_status='failed', lookup_failure_count=7,
                  last_lookup_datetime=_RECENT, manual_refresh_requested=1)
    with _env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE) as m:
        assert _poll_manual(db) is True

    m.off.assert_called_once_with(_BARCODE)
    assert _variant(db)["lookup_status"] == "resolved"


def test_manual_ignores_stock_gate(db):
    """3d added a stock predicate to poll 3's background selection queries only. The manual path's own
    query has no join to inventory at all, so an out-of-stock (no inventory row), no-minimum variant
    is still processed on request — unlike poll 3, which would now exclude it."""
    _seed_variant(db, lookup_status='pending', minimum_quantity=0, manual_refresh_requested=1)
    with _env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE) as m:
        assert _poll_manual(db) is True

    m.off.assert_called_once_with(_BARCODE)
    pv = _variant(db)
    assert pv["lookup_status"] == "resolved"
    assert pv["manual_refresh_requested"] == 0


# ---------------------------------------------------------------------------
# Pacing + stamping
# ---------------------------------------------------------------------------

def test_manual_paces_before_off_and_stamps_state(db):
    """_pace_off_call runs before the OFF call, and worker_state.off_last_called_at is stamped."""
    _seed_variant(db, lookup_status='pending', manual_refresh_requested=1)
    order = []
    with patch("src.worker.main.get_connection", _make_get_connection(db)), \
         patch("src.worker.main.datetime") as mock_dt, \
         patch("src.worker.main.time.sleep"), \
         patch("src.worker.main._pace_off_call", side_effect=lambda db: order.append("pace")), \
         patch("src.worker.off.lookup_barcode", side_effect=lambda bc: order.append("off") or _GOOD_OFF), \
         patch("src.worker.sainsburys.get_price", return_value=_GOOD_PRICE):
        mock_dt.now.return_value = _NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        _poll_manual(db)

    assert order[:2] == ["pace", "off"]
    stamp = db.execute("SELECT off_last_called_at FROM worker_state WHERE id = 1").fetchone()[0]
    assert stamp == _NOW.isoformat()


# ---------------------------------------------------------------------------
# Marker cleared atomically (both outcomes)
# ---------------------------------------------------------------------------

def test_manual_marker_cleared_on_success_and_failure(db):
    _seed_variant(db, barcode=_BARCODE, lookup_status='pending', manual_refresh_requested=1)
    with _env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE):
        _poll_manual(db)
    assert _variant(db, _BARCODE)["manual_refresh_requested"] == 0

    _seed_variant(db, barcode=_BARCODE2, lookup_status='pending', manual_refresh_requested=1)
    with _env(db, off_result=None):
        _poll_manual(db)
    assert _variant(db, _BARCODE2)["manual_refresh_requested"] == 0


def test_manual_no_marked_row_is_noop(db):
    _seed_variant(db, lookup_status='pending', manual_refresh_requested=0)
    with _env(db, off_result=_GOOD_OFF) as m:
        assert _poll_manual(db) is False
    m.off.assert_not_called()


# ---------------------------------------------------------------------------
# Path order within _poll_iteration
# ---------------------------------------------------------------------------

def test_poll1_preempts_manual(db):
    """A pending session item (poll 1) is processed before any manual refresh; the marker is left set."""
    _seed_session_item(db, barcode=_BARCODE2, info_status='pending', price_status='pending')
    _seed_variant(db, barcode=_BARCODE, lookup_status='pending', manual_refresh_requested=1)
    with _env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE) as m:
        assert _poll_iteration(db) is True

    m.off.assert_called_once_with(_BARCODE2)  # the session item, not the marked variant
    assert _variant(db, _BARCODE)["manual_refresh_requested"] == 1  # untouched this tick


def test_poll2_preempts_manual(db):
    """A price-pending session item (poll 2) is processed before any manual refresh."""
    _seed_variant(db, barcode=_BARCODE2, name='Soup', lookup_status='resolved')
    _seed_session_item(db, barcode=_BARCODE2, info_status='resolved', price_status='pending')
    _seed_variant(db, barcode=_BARCODE, lookup_status='pending', manual_refresh_requested=1)
    with _env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE) as m:
        assert _poll_iteration(db) is True

    m.price.assert_called_once()  # poll 2 ran
    m.off.assert_not_called()     # no OFF call this tick (poll 2 needs no OFF)
    assert _variant(db, _BARCODE)["manual_refresh_requested"] == 1


def test_manual_runs_before_poll3_and_returns(db):
    """With no live work, the marked row is processed before poll 3, one unit then return: the
    poll-3-eligible failed row is NOT also touched this tick."""
    _seed_variant(db, barcode=_BARCODE, lookup_status='pending', manual_refresh_requested=1)
    # A separate failed+stale variant that poll 3 would otherwise pick up.
    _seed_variant(db, barcode=_BARCODE2, lookup_status='failed', lookup_failure_count=1,
                  last_lookup_datetime=_iso(_NOW - timedelta(hours=48)))
    with _env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE) as m:
        assert _poll_iteration(db) is True

    m.off.assert_called_once_with(_BARCODE)  # only the marked row; poll 3 never ran
    assert _variant(db, _BARCODE)["manual_refresh_requested"] == 0
    assert _variant(db, _BARCODE2)["lookup_status"] == "failed"  # poll-3 row untouched
