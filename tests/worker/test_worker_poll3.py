"""Tests for the poll-3 background retry/refresh path in src/worker/main.py.

Poll 3 is the strictly-lowest-priority worker path: it retries failed OFF/price lookups and refreshes
stale resolved prices off the durable lookup columns, never touching session_items or the confirm gate.
All time-dependent behaviour is made deterministic by patching ``src.worker.main.datetime`` to a fixed
"now"; OFF/Sainsbury's clients are mocked.
"""

import sqlite3
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from src.worker.main import (
    LOOKUP_FAILURE_CAP,
    _attempt_price,
    _poll3,
    _poll_iteration,
)

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
CREATE TABLE product_groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, minimum_quantity INTEGER NOT NULL DEFAULT 0);
CREATE TABLE group_variant_members (group_id INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE, barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, PRIMARY KEY (group_id, barcode, retailer_id), FOREIGN KEY (barcode, retailer_id) REFERENCES product_variants(barcode, retailer_id) ON DELETE CASCADE);
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO worker_state (id, off_last_called_at) VALUES (1, '1970-01-01T00:00:00');
"""

_BARCODE = "5014788110140"
_BARCODE2 = "5000000000000"
_RETAILER_ID = 1
_SESSION_ID = 1
_GOOD_OFF = {"name": "Baked Beans", "brand": "Heinz", "product_quantity": "415g"}
_GOOD_PRICE = {"price_pence": 85, "price_type": "unit", "product_url": "https://x/p"}

# Fixed injected clock. Boundaries derive from this in the module under test.
_NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec='seconds')


_NOW_ISO = _iso(_NOW)
_DUE_24H = _iso(_NOW - timedelta(hours=25))       # older than 24h -> due
_NOT_DUE_24H = _iso(_NOW - timedelta(hours=1))    # within 24h -> not due
_DUE_30D = _iso(_NOW - timedelta(days=31))        # older than 30d -> due
_NOT_DUE_30D = _iso(_NOW - timedelta(days=1))     # within 30d -> not due


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
                  lookup_failure_count=0, last_lookup_datetime=None):
    conn.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (barcode,))
    conn.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name, brand, product_quantity, "
        "minimum_quantity, lookup_status, lookup_failure_count, last_lookup_datetime) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (barcode, retailer_id, name, brand, product_quantity, minimum_quantity,
         lookup_status, lookup_failure_count, last_lookup_datetime),
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


def _seed_inventory(conn, *, barcode=_BARCODE, retailer_id=_RETAILER_ID, quantity=1):
    conn.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (barcode,))
    conn.execute(
        "INSERT INTO inventory (barcode, retailer_id, quantity) VALUES (?, ?, ?)",
        (barcode, retailer_id, quantity),
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
def _poll3_env(db, *, off_result=None, price_result=None, now=_NOW):
    """Patch the module's clock, connection factory, pacing, and network clients for a poll-3 run."""
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


# ---------------------------------------------------------------------------
# (a) OFF retry — selection
# ---------------------------------------------------------------------------

def test_off_retry_selects_failed_due(db):
    _seed_variant(db, lookup_status='failed', lookup_failure_count=1, last_lookup_datetime=_DUE_24H)
    _seed_inventory(db)
    with _poll3_env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is True
    m.off.assert_called_once_with(_BARCODE)
    pv = db.execute(
        "SELECT lookup_status FROM product_variants WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert pv["lookup_status"] == "resolved"


def test_off_retry_null_datetime_due(db):
    _seed_variant(db, lookup_status='failed', lookup_failure_count=1, last_lookup_datetime=None)
    _seed_inventory(db)
    with _poll3_env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is True
    m.off.assert_called_once_with(_BARCODE)


def test_off_retry_not_due_excluded(db):
    # Recently stamped (well inside 24h) -> not due. Pins the boundary direction: an inverted
    # comparison would select this and the test would fail.
    _seed_variant(db, lookup_status='failed', lookup_failure_count=1, last_lookup_datetime=_NOT_DUE_24H)
    _seed_inventory(db)  # in stock, so exclusion here is purely the not-due boundary
    with _poll3_env(db, off_result=_GOOD_OFF) as m:
        did = _poll3(db)

    assert did is False
    m.off.assert_not_called()


def test_off_resolved_never_reselected(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved', last_lookup_datetime=_DUE_24H)
    _seed_inventory(db)  # in stock, so exclusion here is purely the resolved-status guard
    with _poll3_env(db, off_result=_GOOD_OFF) as m:
        did = _poll3(db)

    assert did is False
    m.off.assert_not_called()


def test_off_pending_not_selected(db):
    _seed_variant(db, lookup_status='pending', last_lookup_datetime=None)
    _seed_inventory(db)  # in stock, so exclusion here is purely the pending-status guard
    with _poll3_env(db, off_result=_GOOD_OFF) as m:
        did = _poll3(db)

    assert did is False
    m.off.assert_not_called()


def test_off_retry_cap_excludes_at_cap(db):
    _seed_variant(db, lookup_status='failed', lookup_failure_count=LOOKUP_FAILURE_CAP,
                  last_lookup_datetime=_DUE_24H)
    _seed_inventory(db)  # in stock, so exclusion here is purely the failure-cap boundary
    with _poll3_env(db, off_result=_GOOD_OFF) as m:
        did = _poll3(db)

    assert did is False
    m.off.assert_not_called()


def test_off_retry_cap_edge_below_selected(db):
    # count == CAP - 1 must still be eligible (pins the `< cap` edge).
    _seed_variant(db, lookup_status='failed', lookup_failure_count=LOOKUP_FAILURE_CAP - 1,
                  last_lookup_datetime=_DUE_24H)
    _seed_inventory(db)
    with _poll3_env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is True
    m.off.assert_called_once_with(_BARCODE)


# ---------------------------------------------------------------------------
# (a) OFF retry — write semantics, pacing, chaining
# ---------------------------------------------------------------------------

def test_off_retry_success_writes_durable_no_session(db):
    _seed_variant(db, lookup_status='failed', lookup_failure_count=3, last_lookup_datetime=_DUE_24H)
    _seed_inventory(db)
    _seed_session_item(db, info_status='pending', price_status='pending')  # must stay untouched

    with _poll3_env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE):
        _poll3(db)

    pv = db.execute(
        "SELECT name, lookup_status, lookup_failure_count, last_lookup_datetime "
        "FROM product_variants WHERE barcode = ? AND retailer_id = ?", (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pv["name"] == "Baked Beans"
    assert pv["lookup_status"] == "resolved"
    assert pv["lookup_failure_count"] == 0
    assert pv["last_lookup_datetime"] == _NOW_ISO  # fresh, 1-second resolution

    si = db.execute(
        "SELECT info_status, price_status FROM session_items WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert si["info_status"] == "pending"
    assert si["price_status"] == "pending"


def test_off_retry_failure_preserves_and_counts(db):
    # A previously-cached failed row (has name/brand/qty + a user minimum). A failed retry must keep
    # all of that, stay 'failed', and increment the count.
    # minimum_quantity=4 also incidentally satisfies the 3d stock gate (no separate _seed_inventory
    # needed) — this test's own subject is failure-preservation, not the gate.
    _seed_variant(db, name="Old Beans", brand="Heinz", product_quantity="415g", minimum_quantity=4,
                  lookup_status='failed', lookup_failure_count=2, last_lookup_datetime=_DUE_24H)

    with _poll3_env(db, off_result=None) as m:
        _poll3(db)

    pv = db.execute(
        "SELECT name, brand, product_quantity, minimum_quantity, lookup_status, lookup_failure_count "
        "FROM product_variants WHERE barcode = ? AND retailer_id = ?", (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pv["name"] == "Old Beans"
    assert pv["brand"] == "Heinz"
    assert pv["product_quantity"] == "415g"
    assert pv["minimum_quantity"] == 4
    assert pv["lookup_status"] == "failed"
    assert pv["lookup_failure_count"] == 3
    m.price.assert_not_called()  # failure never chains a price attempt


def test_off_retry_nameless_result_is_failure(db):
    # OFF returns a dict with a null name: must collapse to failure (not written 'resolved') and must
    # not chain a price attempt.
    _seed_variant(db, lookup_status='failed', lookup_failure_count=1, last_lookup_datetime=_DUE_24H)
    _seed_inventory(db)
    with _poll3_env(db, off_result={"name": None, "brand": None, "product_quantity": None}) as m:
        _poll3(db)

    pv = db.execute(
        "SELECT lookup_status, lookup_failure_count FROM product_variants WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert pv["lookup_status"] == "failed"
    assert pv["lookup_failure_count"] == 2
    m.price.assert_not_called()


def test_off_retry_is_paced(db):
    # _pace_off_call must run before the OFF network call.
    _seed_variant(db, lookup_status='failed', lookup_failure_count=0, last_lookup_datetime=_DUE_24H)
    _seed_inventory(db)
    order = []
    with patch("src.worker.main.get_connection", _make_get_connection(db)), \
         patch("src.worker.main.datetime") as mock_dt, \
         patch("src.worker.main.time.sleep"), \
         patch("src.worker.main._pace_off_call", side_effect=lambda db: order.append("pace")), \
         patch("src.worker.off.lookup_barcode", side_effect=lambda bc: order.append("off") or _GOOD_OFF), \
         patch("src.worker.sainsburys.get_price", return_value=_GOOD_PRICE):
        mock_dt.now.return_value = _NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        _poll3(db)

    assert order[:2] == ["pace", "off"]


def test_off_retry_price_chaining(db):
    # OFF resolves and there is NO prices row -> chain an initial price attempt in the same iteration.
    _seed_variant(db, lookup_status='failed', lookup_failure_count=1, last_lookup_datetime=_DUE_24H)
    _seed_inventory(db)
    with _poll3_env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE) as m:
        _poll3(db)

    m.price.assert_called_once()
    assert m.price.call_args.kwargs["name"] == "Baked Beans"
    pr = db.execute(
        "SELECT price_pence, lookup_status FROM prices WHERE barcode = ? AND retailer_id = ?",
        (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pr["price_pence"] == 85
    assert pr["lookup_status"] == "resolved"


def test_off_retry_success_does_not_chain_when_price_row_exists(db):
    # A prices row already exists (failed). It is owned by the price-retry path (b); the OFF retry must
    # NOT re-attempt the price in this iteration.
    _seed_variant(db, lookup_status='failed', lookup_failure_count=1, last_lookup_datetime=_DUE_24H)
    _seed_inventory(db)
    _seed_price(db, lookup_status='failed', lookup_failure_count=1, last_lookup_datetime=_NOW_ISO)

    with _poll3_env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE) as m:
        _poll3(db)

    m.price.assert_not_called()


# ---------------------------------------------------------------------------
# (b) Price retry
# ---------------------------------------------------------------------------

def test_price_retry_selects_failed_due(db):
    _seed_variant(db, name="Baked Beans", brand="Heinz", product_quantity="415g",
                  lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db)
    _seed_price(db, price_pence=None, lookup_status='failed', lookup_failure_count=1,
                last_lookup_datetime=_DUE_24H)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is True
    m.price.assert_called_once()
    pr = db.execute("SELECT lookup_status FROM prices WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert pr["lookup_status"] == "resolved"


def test_price_retry_null_datetime_due(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db)
    _seed_price(db, lookup_status='failed', lookup_failure_count=1, last_lookup_datetime=None)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is True
    m.price.assert_called_once()


def test_price_retry_not_due_excluded(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db)  # in stock, so exclusion here is purely the not-due boundary
    _seed_price(db, lookup_status='failed', lookup_failure_count=1, last_lookup_datetime=_NOT_DUE_24H)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is False
    m.price.assert_not_called()


def test_price_retry_cap_excludes_at_cap(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db)  # in stock, so exclusion here is purely the failure-cap boundary
    _seed_price(db, lookup_status='failed', lookup_failure_count=LOOKUP_FAILURE_CAP,
                last_lookup_datetime=_DUE_24H)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is False
    m.price.assert_not_called()


def test_price_retry_cap_edge_below_selected(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db)
    _seed_price(db, lookup_status='failed', lookup_failure_count=LOOKUP_FAILURE_CAP - 1,
                last_lookup_datetime=_DUE_24H)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is True
    m.price.assert_called_once()


def test_price_retry_not_paced(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db)
    _seed_price(db, lookup_status='failed', lookup_failure_count=1, last_lookup_datetime=_DUE_24H)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        _poll3(db)

    m.pace.assert_not_called()
    m.off.assert_not_called()


def test_price_failure_preserves_and_counts(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db)
    _seed_price(db, price_pence=85, price_type='unit', product_url='https://old',
                lookup_status='failed', lookup_failure_count=1, last_lookup_datetime=_DUE_24H)

    with _poll3_env(db, price_result=None):
        _poll3(db)

    pr = db.execute(
        "SELECT price_pence, price_type, product_url, lookup_status, lookup_failure_count "
        "FROM prices WHERE barcode = ? AND retailer_id = ?", (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pr["price_pence"] == 85
    assert pr["price_type"] == "unit"
    assert pr["product_url"] == "https://old"
    assert pr["lookup_status"] == "failed"
    assert pr["lookup_failure_count"] == 2


def test_price_retry_success_resets_and_stamps(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db)
    _seed_price(db, price_pence=85, lookup_status='failed', lookup_failure_count=2,
                last_lookup_datetime=_DUE_24H)

    with _poll3_env(db, price_result=_GOOD_PRICE):
        _poll3(db)

    pr = db.execute(
        "SELECT lookup_status, lookup_failure_count, last_lookup_datetime "
        "FROM prices WHERE barcode = ? AND retailer_id = ?", (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pr["lookup_status"] == "resolved"
    assert pr["lookup_failure_count"] == 0
    assert pr["last_lookup_datetime"] == _NOW_ISO


# ---------------------------------------------------------------------------
# (c) Price refresh
# ---------------------------------------------------------------------------

def test_price_refresh_selects_resolved_due(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db)
    _seed_price(db, price_pence=85, price_type='unit', lookup_status='resolved',
                lookup_failure_count=0, last_lookup_datetime=_DUE_30D)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is True
    m.price.assert_called_once()


def test_price_refresh_null_datetime_due(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db)
    _seed_price(db, price_pence=85, lookup_status='resolved', last_lookup_datetime=None)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is True
    m.price.assert_called_once()


def test_price_refresh_not_due_excluded(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db)  # in stock, so exclusion here is purely the not-due boundary
    _seed_price(db, price_pence=85, lookup_status='resolved', last_lookup_datetime=_NOT_DUE_30D)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is False
    m.price.assert_not_called()


def test_price_refresh_skips_per_kg(db):
    _seed_variant(db, name="Loose Bananas", lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db)  # in stock, so exclusion here is purely the per_kg guard
    _seed_price(db, price_pence=120, price_type='per_kg', lookup_status='resolved',
                last_lookup_datetime=_DUE_30D)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is False
    m.price.assert_not_called()


# ---------------------------------------------------------------------------
# Stock gate (3d) — out-of-stock, no-minimum variants are excluded from all three paths;
# low-stock-with-minimum and present-but-zero-quantity are pinned explicitly.
# ---------------------------------------------------------------------------

def test_off_retry_excluded_no_inventory_row(db):
    # Otherwise-eligible (failed/due/under-cap), but no inventory row at all and no minimum override.
    _seed_variant(db, lookup_status='failed', lookup_failure_count=1, last_lookup_datetime=_DUE_24H)
    with _poll3_env(db, off_result=_GOOD_OFF) as m:
        did = _poll3(db)

    assert did is False
    m.off.assert_not_called()


def test_price_retry_excluded_no_inventory_row(db):
    # Otherwise-eligible (failed/due/under-cap), but no inventory row at all and no minimum override.
    _seed_variant(db, name="Baked Beans", lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_price(db, lookup_status='failed', lookup_failure_count=1, last_lookup_datetime=_DUE_24H)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is False
    m.price.assert_not_called()


def test_price_refresh_excluded_no_inventory_row(db):
    # Otherwise-eligible (resolved/due/non-per_kg), but no inventory row at all and no minimum override.
    _seed_variant(db, name="Baked Beans", lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_price(db, price_pence=85, price_type='unit', lookup_status='resolved',
                last_lookup_datetime=_DUE_30D)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is False
    m.price.assert_not_called()


def test_price_refresh_selects_low_stock_with_minimum(db):
    # Zero stock but a user-set minimum keeps the variant eligible (low-stock shopping-list case).
    _seed_variant(db, name="Baked Beans", minimum_quantity=2, lookup_status='resolved',
                  last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db, quantity=0)
    _seed_price(db, price_pence=85, price_type='unit', lookup_status='resolved',
                last_lookup_datetime=_DUE_30D)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is True
    m.price.assert_called_once()


def test_price_refresh_excluded_zero_quantity_present_no_minimum(db):
    # Pins the `quantity > 0` boundary on its own terms: an inventory row IS present (not NULL via the
    # LEFT JOIN), its quantity is exactly zero, and there is no minimum override. A `>= 0` slip would
    # pass this row through and this test would catch it, whereas the "no inventory row" tests above
    # only exercise the NULL arm and the low-stock test above only exercises quantity=1/minimum>0.
    _seed_variant(db, name="Baked Beans", lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db, quantity=0)
    _seed_price(db, price_pence=85, price_type='unit', lookup_status='resolved',
                last_lookup_datetime=_DUE_30D)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is False
    m.price.assert_not_called()


# ---------------------------------------------------------------------------
# Null-name guard (§3.1b/c + helper)
# ---------------------------------------------------------------------------

def test_price_null_name_variant_not_selected(db):
    # A failed price whose variant has a null name is excluded by the selection JOIN, so no scraper
    # call is issued.
    _seed_variant(db, name=None, lookup_status='failed', lookup_failure_count=1,
                  last_lookup_datetime=_NOW_ISO)
    _seed_inventory(db)  # in stock, so exclusion here is purely the null-name guard
    _seed_price(db, lookup_status='failed', lookup_failure_count=1, last_lookup_datetime=_DUE_24H)

    with _poll3_env(db, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is False
    m.price.assert_not_called()


def test_attempt_price_null_name_skips_scraper(db):
    # The helper's own runtime guard: a null/empty name never fires a Sainsbury's query and writes no
    # prices row.
    _seed_variant(db, name=None, lookup_status='failed', lookup_failure_count=1)
    with patch("src.worker.main.get_connection", _make_get_connection(db)), \
         patch("src.worker.sainsburys.get_price") as mock_price:
        _attempt_price(_BARCODE, _RETAILER_ID, None, None, None)

    mock_price.assert_not_called()
    assert db.execute("SELECT COUNT(*) AS c FROM prices WHERE barcode = ?", (_BARCODE,)).fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# Priority, one-unit-per-tick, session isolation
# ---------------------------------------------------------------------------

def test_poll3_skipped_when_poll1_has_work(db):
    # A live info-pending item (poll 1) plus a separate poll-3-eligible failed OFF row. Poll 1 wins;
    # the poll-3 row must be untouched this tick.
    _seed_session_item(db, barcode=_BARCODE, info_status='pending', price_status='pending')
    _seed_variant(db, barcode=_BARCODE2, lookup_status='failed', lookup_failure_count=1,
                  last_lookup_datetime=_DUE_24H)

    with patch("src.worker.main.get_connection", _make_get_connection(db)), \
         patch("src.worker.main.datetime") as mock_dt, \
         patch("src.worker.main.time.sleep"), \
         patch("src.worker.main._pace_off_call"), \
         patch("src.worker.off.lookup_barcode", return_value=_GOOD_OFF), \
         patch("src.worker.sainsburys.get_price", return_value=_GOOD_PRICE):
        mock_dt.now.return_value = _NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        did = _poll_iteration(db)

    assert did is True
    pv2 = db.execute(
        "SELECT lookup_status, lookup_failure_count, last_lookup_datetime "
        "FROM product_variants WHERE barcode = ?", (_BARCODE2,)
    ).fetchone()
    assert pv2["lookup_status"] == "failed"
    assert pv2["lookup_failure_count"] == 1
    assert pv2["last_lookup_datetime"] == _DUE_24H  # untouched


def test_poll3_skipped_when_poll2_has_work(db):
    # A live price-pending item (poll 2) plus a separate poll-3-eligible failed price. Poll 2 wins.
    _seed_variant(db, barcode=_BARCODE, name="Baked Beans", brand="Heinz", product_quantity="415g",
                  lookup_status='resolved', last_lookup_datetime=_NOW_ISO)
    _seed_session_item(db, barcode=_BARCODE, info_status='resolved', price_status='pending')
    _seed_variant(db, barcode=_BARCODE2, name="Other", lookup_status='resolved',
                  last_lookup_datetime=_NOW_ISO)
    _seed_price(db, barcode=_BARCODE2, lookup_status='failed', lookup_failure_count=1,
                last_lookup_datetime=_DUE_24H)

    with patch("src.worker.main.get_connection", _make_get_connection(db)), \
         patch("src.worker.main.datetime") as mock_dt, \
         patch("src.worker.main.time.sleep"), \
         patch("src.worker.main._pace_off_call") as mock_pace, \
         patch("src.worker.sainsburys.get_price", return_value=_GOOD_PRICE) as mock_price:
        mock_dt.now.return_value = _NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        did = _poll_iteration(db)

    assert did is True
    mock_pace.assert_not_called()          # poll 2 does no OFF work
    assert mock_price.call_count == 1       # only the poll-2 item, not the poll-3 row
    pr2 = db.execute(
        "SELECT lookup_status, lookup_failure_count FROM prices WHERE barcode = ?", (_BARCODE2,)
    ).fetchone()
    assert pr2["lookup_status"] == "failed"
    assert pr2["lookup_failure_count"] == 1  # untouched


def test_poll3_one_unit_per_iteration(db):
    # Two OFF-retry-eligible rows; exactly one is processed per poll-3 call.
    _seed_variant(db, barcode=_BARCODE, lookup_status='failed', lookup_failure_count=1,
                  last_lookup_datetime=_DUE_24H)
    _seed_inventory(db, barcode=_BARCODE)
    _seed_variant(db, barcode=_BARCODE2, lookup_status='failed', lookup_failure_count=1,
                  last_lookup_datetime=_DUE_24H)
    _seed_inventory(db, barcode=_BARCODE2)

    with _poll3_env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is True
    assert m.off.call_count == 1


def test_poll3_never_touches_session_items(db):
    # A session_items row (any state) plus a poll-3-eligible OFF row for a different barcode. Poll 3
    # must leave session_items completely alone.
    _seed_session_item(db, barcode=_BARCODE, info_status='pending', price_status='pending')
    _seed_variant(db, barcode=_BARCODE2, lookup_status='failed', lookup_failure_count=1,
                  last_lookup_datetime=_DUE_24H)
    _seed_inventory(db, barcode=_BARCODE2)

    with _poll3_env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE):
        did = _poll3(db)

    assert did is True
    si = db.execute(
        "SELECT info_status, price_status FROM session_items WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert si["info_status"] == "pending"
    assert si["price_status"] == "pending"


def test_poll3_returns_false_when_no_work(db):
    with _poll3_env(db, off_result=_GOOD_OFF, price_result=_GOOD_PRICE) as m:
        did = _poll3(db)

    assert did is False
    m.off.assert_not_called()
    m.price.assert_not_called()
