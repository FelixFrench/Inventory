"""Tests for src/worker/main.py — OFF/Sainsbury's write-back, durable lookup state, worker
state, OFF pacing, composite re-key, and the null-name poll-2 guard."""

import sqlite3
from datetime import datetime, timedelta, UTC
from unittest.mock import MagicMock, patch

import pytest

from src.worker.main import (
    OFF_RATE_LIMIT_SECS,
    _compute_startup_sleep,
    _pace_off_call,
    _phase1_failure,
    _phase1_success,
    _phase2_failure,
    _phase2_not_possible,
    _phase2_success,
    _poll_forever,
    _poll_iteration,
    _upsert_price_durable,
    _upsert_variant_durable,
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
CREATE TABLE inventory (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE sessions (id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('in', 'out')), started_at TEXT NOT NULL, recovered_at TEXT);
CREATE TABLE session_items (session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL, delta INTEGER NOT NULL CHECK(delta >= 0), info_status TEXT NOT NULL DEFAULT 'pending' CHECK(info_status IN ('pending', 'resolved', 'failed')), price_status TEXT NOT NULL DEFAULT 'pending' CHECK(price_status IN ('pending', 'resolved', 'failed', 'not_possible')), first_scanned_at TEXT NOT NULL, PRIMARY KEY (session_id, barcode, retailer_id));
CREATE INDEX idx_session_items_info_pending ON session_items(first_scanned_at) WHERE info_status = 'pending';
CREATE INDEX idx_session_items_price_pending ON session_items(first_scanned_at) WHERE price_status = 'pending';
CREATE TABLE worker_state (id INTEGER PRIMARY KEY CHECK(id = 1), off_last_called_at TEXT NOT NULL);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE product_groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, minimum_quantity INTEGER NOT NULL DEFAULT 0);
CREATE TABLE group_variant_members (group_id INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE, barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, PRIMARY KEY (group_id, barcode, retailer_id), FOREIGN KEY (barcode, retailer_id) REFERENCES product_variants(barcode, retailer_id) ON DELETE CASCADE);
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO retailers (name, scraper_class) VALUES ('Other', 'OtherProvider');
INSERT INTO worker_state (id, off_last_called_at) VALUES (1, '1970-01-01T00:00:00');
"""

_BARCODE = "5014788110140"
_RETAILER_ID = 1
_OTHER_RETAILER_ID = 2
_SESSION_ID = 1
_GOOD_OFF = {"name": "Baked Beans", "brand": "Heinz", "product_quantity": "415g"}
_GOOD_PRICE = {"price_pence": 85, "price_type": "unit", "product_url": "https://www.sainsburys.co.uk/gol-ui/product/test"}
_GOOD_PRICE_NO_URL = {"price_pence": 85, "price_type": "unit", "product_url": None}


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def _seed(conn: sqlite3.Connection, info_status: str = "pending", price_status: str = "pending",
          session_id: int = _SESSION_ID, retailer_id: int = _RETAILER_ID):
    conn.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, type, started_at) VALUES (?, 'in', '2026-05-27T10:00:00')",
        (session_id,)
    )
    conn.execute(
        "INSERT INTO session_items (session_id, barcode, retailer_id, delta, info_status, price_status, first_scanned_at) "
        "VALUES (?, ?, ?, 1, ?, ?, '2026-05-27T10:00:00')",
        (session_id, _BARCODE, retailer_id, info_status, price_status)
    )
    conn.commit()


def _seed_variant(conn: sqlite3.Connection, name=None, brand=None, product_quantity=None,
                  minimum_quantity=0, lookup_status='pending', lookup_failure_count=0,
                  retailer_id: int = _RETAILER_ID):
    conn.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    conn.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name, brand, product_quantity, "
        "minimum_quantity, lookup_status, lookup_failure_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (_BARCODE, retailer_id, name, brand, product_quantity, minimum_quantity,
         lookup_status, lookup_failure_count)
    )
    conn.commit()


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

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        return False


def _make_get_connection(conn: sqlite3.Connection):
    return MagicMock(return_value=_MockConn(conn))


# ---------------------------------------------------------------------------
# OFF failure handling
# ---------------------------------------------------------------------------

def test_off_failure_sets_info_failed_and_price_not_possible(db):
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        rowcount = _phase1_failure(_BARCODE, _SESSION_ID, _RETAILER_ID)

    assert rowcount == 1
    row = db.execute(
        "SELECT info_status, price_status FROM session_items WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert row["info_status"] == "failed"
    assert row["price_status"] == "not_possible"


def test_off_failure_price_status_is_not_possible_not_failed(db):
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase1_failure(_BARCODE, _SESSION_ID, _RETAILER_ID)

    row = db.execute(
        "SELECT price_status FROM session_items WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert row["price_status"] == "not_possible"
    assert row["price_status"] != "failed"


def test_off_failure_writes_null_data_variant_row(db):
    # Always-write-row (3a): an OFF failure now writes a durable product_variants row with null
    # OFF data, lookup_status='failed', failure count 1, and a timestamp; NO prices row.
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase1_failure(_BARCODE, _SESSION_ID, _RETAILER_ID)

    pv = db.execute(
        "SELECT name, brand, product_quantity, lookup_status, lookup_failure_count, "
        "last_lookup_datetime FROM product_variants WHERE barcode = ? AND retailer_id = ?",
        (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pv is not None
    assert pv["name"] is None
    assert pv["brand"] is None
    assert pv["product_quantity"] is None
    assert pv["lookup_status"] == "failed"
    assert pv["lookup_failure_count"] == 1
    assert pv["last_lookup_datetime"] is not None
    assert "." not in pv["last_lookup_datetime"]  # 1-second resolution, no microseconds

    prices = db.execute("SELECT COUNT(*) AS c FROM prices WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert prices["c"] == 0


def test_off_failure_preserves_minimum_and_group_memberships(db):
    # A minimum-only upsert row (null name, minimum set) already exists AND belongs to a group.
    # An OFF-failure upsert must NOT reset minimum_quantity nor drop the group membership — proven
    # by using ON CONFLICT (not INSERT OR REPLACE, which would delete+reinsert and cascade-drop
    # the membership).
    _seed_variant(db, minimum_quantity=7, lookup_status='pending')
    db.execute("INSERT INTO product_groups (id, name) VALUES (1, 'Beans')")
    db.execute(
        "INSERT INTO group_variant_members (group_id, barcode, retailer_id) VALUES (1, ?, ?)",
        (_BARCODE, _RETAILER_ID)
    )
    db.commit()
    _seed(db)

    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase1_failure(_BARCODE, _SESSION_ID, _RETAILER_ID)

    pv = db.execute(
        "SELECT minimum_quantity, name, lookup_status, lookup_failure_count "
        "FROM product_variants WHERE barcode = ? AND retailer_id = ?", (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pv["minimum_quantity"] == 7   # preserved
    assert pv["name"] is None            # still null-data
    assert pv["lookup_status"] == "failed"
    assert pv["lookup_failure_count"] == 1

    members = db.execute(
        "SELECT COUNT(*) AS c FROM group_variant_members WHERE barcode = ? AND retailer_id = ?",
        (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert members["c"] == 1             # membership survived


def test_off_failure_increments_existing_failure_count(db):
    # A prior failed row (count 2) that fails again increments to 3 via the ON CONFLICT branch,
    # leaving name/minimum untouched.
    _seed_variant(db, minimum_quantity=3, lookup_status='failed', lookup_failure_count=2)
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase1_failure(_BARCODE, _SESSION_ID, _RETAILER_ID)

    pv = db.execute(
        "SELECT lookup_failure_count, minimum_quantity FROM product_variants "
        "WHERE barcode = ? AND retailer_id = ?", (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pv["lookup_failure_count"] == 3
    assert pv["minimum_quantity"] == 3


# ---------------------------------------------------------------------------
# OFF success write-back
# ---------------------------------------------------------------------------

def test_off_success_writes_durable_columns(db):
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        rowcount = _phase1_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_OFF)

    assert rowcount == 1
    pv = db.execute(
        "SELECT name, brand, product_quantity, lookup_status, lookup_failure_count, "
        "last_lookup_datetime FROM product_variants WHERE barcode = ? AND retailer_id = ?",
        (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pv["name"] == "Baked Beans"
    assert pv["brand"] == "Heinz"
    assert pv["product_quantity"] == "415g"
    assert pv["lookup_status"] == "resolved"
    assert pv["lookup_failure_count"] == 0
    assert pv["last_lookup_datetime"] is not None
    assert "." not in pv["last_lookup_datetime"]

    si = db.execute(
        "SELECT info_status FROM session_items WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert si["info_status"] == "resolved"


def test_off_success_preserves_minimum_on_conflict(db):
    # OFF success onto a pre-existing minimum-only row must not reset minimum_quantity
    # (fixes the old INSERT OR REPLACE clobber) and resets the failure count to 0.
    _seed_variant(db, minimum_quantity=5, lookup_status='failed', lookup_failure_count=4)
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase1_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_OFF)

    pv = db.execute(
        "SELECT name, minimum_quantity, lookup_status, lookup_failure_count "
        "FROM product_variants WHERE barcode = ? AND retailer_id = ?", (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pv["name"] == "Baked Beans"
    assert pv["minimum_quantity"] == 5       # preserved
    assert pv["lookup_status"] == "resolved"
    assert pv["lookup_failure_count"] == 0   # reset


def test_off_success_rowcount_zero_when_session_discarded(db):
    # Cache-retention: when the session was discarded mid-lookup the session_items UPDATE affects
    # zero rows, but the durable product_variants row is still committed as a cache warm-up.
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.commit()

    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        rowcount = _phase1_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_OFF)

    assert rowcount == 0
    pv = db.execute(
        "SELECT name, lookup_status FROM product_variants WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert pv is not None
    assert pv["name"] == "Baked Beans"
    assert pv["lookup_status"] == "resolved"


def test_off_failure_rowcount_zero_keeps_variant_row(db):
    # Same cache-warm behaviour on the failure path: durable failed row committed even when the
    # stamp matched no session row.
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.commit()

    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        rowcount = _phase1_failure(_BARCODE, _SESSION_ID, _RETAILER_ID)

    assert rowcount == 0
    pv = db.execute(
        "SELECT lookup_status, lookup_failure_count FROM product_variants WHERE barcode = ?",
        (_BARCODE,)
    ).fetchone()
    assert pv is not None
    assert pv["lookup_status"] == "failed"
    assert pv["lookup_failure_count"] == 1


def test_off_success_updates_worker_state(db):
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase1_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_OFF)

    ts = db.execute(
        "SELECT off_last_called_at FROM worker_state WHERE id = 1"
    ).fetchone()["off_last_called_at"]
    assert ts != "1970-01-01T00:00:00"


def test_off_failure_updates_worker_state(db):
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase1_failure(_BARCODE, _SESSION_ID, _RETAILER_ID)

    ts = db.execute(
        "SELECT off_last_called_at FROM worker_state WHERE id = 1"
    ).fetchone()["off_last_called_at"]
    assert ts != "1970-01-01T00:00:00"


# ---------------------------------------------------------------------------
# Sainsbury's lookup write-back
# ---------------------------------------------------------------------------

def test_price_success_writes_durable_columns(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved')
    _seed(db, info_status="resolved", price_status="pending")
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        rowcount = _phase2_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_PRICE)

    assert rowcount == 1
    pr = db.execute(
        "SELECT price_pence, price_type, product_url, lookup_status, lookup_failure_count, "
        "last_lookup_datetime FROM prices WHERE barcode = ? AND retailer_id = ?",
        (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pr["price_pence"] == 85
    assert pr["lookup_status"] == "resolved"
    assert pr["lookup_failure_count"] == 0
    assert pr["last_lookup_datetime"] is not None
    assert "." not in pr["last_lookup_datetime"]

    si = db.execute("SELECT price_status FROM session_items WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert si["price_status"] == "resolved"


def test_price_failure_writes_row_first_attempt(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved')
    _seed(db, info_status="resolved", price_status="pending")
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        rowcount = _phase2_failure(_BARCODE, _SESSION_ID, _RETAILER_ID)

    assert rowcount == 1
    pr = db.execute(
        "SELECT price_pence, price_type, product_url, lookup_status, lookup_failure_count "
        "FROM prices WHERE barcode = ? AND retailer_id = ?", (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pr["price_pence"] is None
    assert pr["price_type"] == "unit"      # NOT NULL default
    assert pr["product_url"] is None
    assert pr["lookup_status"] == "failed"
    assert pr["lookup_failure_count"] == 1

    si = db.execute("SELECT price_status FROM session_items WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert si["price_status"] == "failed"


def test_price_failure_keeps_existing_price_data_and_increments(db):
    # A previously-resolved price that later fails keeps its price data; only the durable columns
    # change and the failure count increments.
    _seed_variant(db, name="Baked Beans", lookup_status='resolved')
    db.execute(
        "INSERT INTO prices (barcode, retailer_id, price_pence, price_type, product_url, "
        "lookup_status, lookup_failure_count) VALUES (?, ?, 85, 'unit', 'https://x', 'resolved', 0)",
        (_BARCODE, _RETAILER_ID)
    )
    db.commit()
    _seed(db, info_status="resolved", price_status="pending")

    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase2_failure(_BARCODE, _SESSION_ID, _RETAILER_ID)

    pr = db.execute(
        "SELECT price_pence, product_url, lookup_status, lookup_failure_count "
        "FROM prices WHERE barcode = ? AND retailer_id = ?", (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pr["price_pence"] == 85         # kept
    assert pr["product_url"] == "https://x"
    assert pr["lookup_status"] == "failed"
    assert pr["lookup_failure_count"] == 1


def test_sainsburys_success_rowcount_zero_when_session_discarded(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved')

    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        rowcount = _phase2_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_PRICE)

    assert rowcount == 0
    pr = db.execute("SELECT price_pence FROM prices WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert pr is not None
    assert pr["price_pence"] == 85


def test_sainsburys_success_writes_product_url(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved')
    _seed(db, info_status="resolved", price_status="pending")
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase2_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_PRICE)

    pr = db.execute(
        "SELECT price_pence, product_url FROM prices WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert pr["price_pence"] == 85
    assert pr["product_url"] == "https://www.sainsburys.co.uk/gol-ui/product/test"


def test_sainsburys_success_writes_null_product_url_when_none(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved')
    _seed(db, info_status="resolved", price_status="pending")
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase2_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_PRICE_NO_URL)

    pr = db.execute("SELECT product_url FROM prices WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert pr["product_url"] is None


# ---------------------------------------------------------------------------
# Separable durable write (reused by 3b's refresh path, no session stamp)
# ---------------------------------------------------------------------------

def test_upsert_variant_durable_writes_without_session_stamp(db):
    _seed(db)  # session_item stays pending; the durable writer must not touch it
    db.execute("BEGIN IMMEDIATE")
    _upsert_variant_durable(db, _BARCODE, _RETAILER_ID, _GOOD_OFF, "2026-07-21T12:00:00+00:00")
    db.commit()

    pv = db.execute(
        "SELECT name, lookup_status, lookup_failure_count FROM product_variants "
        "WHERE barcode = ? AND retailer_id = ?", (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pv["name"] == "Baked Beans"
    assert pv["lookup_status"] == "resolved"
    assert pv["lookup_failure_count"] == 0

    si = db.execute("SELECT info_status FROM session_items WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert si["info_status"] == "pending"  # untouched — no stamp baked into the durable write


def test_upsert_price_durable_writes_without_session_stamp(db):
    _seed_variant(db, name="Baked Beans", lookup_status='resolved')
    _seed(db, info_status="resolved", price_status="pending")
    db.execute("BEGIN IMMEDIATE")
    _upsert_price_durable(db, _BARCODE, _RETAILER_ID, _GOOD_PRICE, "2026-07-21T12:00:00+00:00")
    db.commit()

    pr = db.execute(
        "SELECT price_pence, lookup_status FROM prices WHERE barcode = ? AND retailer_id = ?",
        (_BARCODE, _RETAILER_ID)
    ).fetchone()
    assert pr["price_pence"] == 85
    assert pr["lookup_status"] == "resolved"

    si = db.execute("SELECT price_status FROM session_items WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert si["price_status"] == "pending"  # untouched


# ---------------------------------------------------------------------------
# Composite re-key: stamps key on (session_id, barcode, retailer_id)
# ---------------------------------------------------------------------------

def test_stamp_is_composite_keyed_on_retailer_id(db):
    # Two session_items for the same (session, barcode) differing only in retailer_id. Stamping
    # for retailer 1 must leave retailer 2's row untouched.
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute("INSERT INTO sessions (id, type, started_at) VALUES (?, 'in', '2026-05-27T10:00:00')", (_SESSION_ID,))
    for rid in (_RETAILER_ID, _OTHER_RETAILER_ID):
        db.execute(
            "INSERT INTO session_items (session_id, barcode, retailer_id, delta, info_status, "
            "price_status, first_scanned_at) VALUES (?, ?, ?, 1, 'resolved', 'pending', '2026-05-27T10:00:00')",
            (_SESSION_ID, _BARCODE, rid)
        )
    # prices FK needs a product_variants row for retailer 1.
    db.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name, lookup_status) VALUES (?, ?, 'Baked Beans', 'resolved')",
        (_BARCODE, _RETAILER_ID)
    )
    db.commit()

    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase2_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_PRICE)

    r1 = db.execute(
        "SELECT price_status FROM session_items WHERE session_id = ? AND barcode = ? AND retailer_id = ?",
        (_SESSION_ID, _BARCODE, _RETAILER_ID)
    ).fetchone()["price_status"]
    r2 = db.execute(
        "SELECT price_status FROM session_items WHERE session_id = ? AND barcode = ? AND retailer_id = ?",
        (_SESSION_ID, _BARCODE, _OTHER_RETAILER_ID)
    ).fetchone()["price_status"]
    assert r1 == "resolved"
    assert r2 == "pending"  # other retailer's row untouched


# ---------------------------------------------------------------------------
# Worker state persistence
# ---------------------------------------------------------------------------

def test_worker_state_survives_session_discard(db):
    _seed(db)
    sentinel = "2026-05-27T10:00:00"
    db.execute("UPDATE worker_state SET off_last_called_at = ? WHERE id = 1", (sentinel,))
    db.commit()

    db.execute("DELETE FROM sessions WHERE id = ?", (_SESSION_ID,))
    db.commit()

    ts = db.execute(
        "SELECT off_last_called_at FROM worker_state WHERE id = 1"
    ).fetchone()["off_last_called_at"]
    assert ts == sentinel


# ---------------------------------------------------------------------------
# Startup sleep
# ---------------------------------------------------------------------------

def test_startup_sleep_required_when_recent_call():
    recent = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    result = _compute_startup_sleep(recent)
    assert result > 0
    assert abs(result - (OFF_RATE_LIMIT_SECS - 1)) < 0.1


def test_startup_no_sleep_when_old_call():
    old = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    result = _compute_startup_sleep(old)
    assert result == 0.0


def test_startup_no_sleep_for_epoch_sentinel():
    result = _compute_startup_sleep("1970-01-01T00:00:00")
    assert result == 0.0


# ---------------------------------------------------------------------------
# In-loop OFF pacing (E4)
# ---------------------------------------------------------------------------

def test_pace_off_call_sleeps_remaining_gap(db):
    # Clock mocked (patched datetime.now fixed; fromisoformat delegated to the real one) so this
    # doesn't depend on wall-clock time. elapsed = 2s ⇒ sleep ≈ 2s.
    fixed_now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    db.execute(
        "UPDATE worker_state SET off_last_called_at = ? WHERE id = 1",
        ((fixed_now - timedelta(seconds=2)).isoformat(),)
    )
    db.commit()

    with patch("src.worker.main.datetime") as mock_dt, \
         patch("src.worker.main.time.sleep") as mock_sleep:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        _pace_off_call(db)

    mock_sleep.assert_called_once()
    slept = mock_sleep.call_args[0][0]
    assert abs(slept - 2.0) < 0.05


def test_pace_off_call_no_sleep_when_gap_elapsed(db):
    fixed_now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    db.execute(
        "UPDATE worker_state SET off_last_called_at = ? WHERE id = 1",
        ((fixed_now - timedelta(seconds=10)).isoformat(),)
    )
    db.commit()

    with patch("src.worker.main.datetime") as mock_dt, \
         patch("src.worker.main.time.sleep") as mock_sleep:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        _pace_off_call(db)

    mock_sleep.assert_not_called()


def test_pace_off_call_no_sleep_for_epoch_seed(db):
    # The naive epoch seed ('1970-01-01T00:00:00') must not raise a naive/aware comparison error
    # and must yield no sleep. Uses the real seed value from the fixture.
    fixed_now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    with patch("src.worker.main.datetime") as mock_dt, \
         patch("src.worker.main.time.sleep") as mock_sleep:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        _pace_off_call(db)

    mock_sleep.assert_not_called()


def test_poll_iteration_paces_before_off_call(db):
    # The full poll-1 path calls _pace_off_call before off.lookup_barcode.
    fixed_now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    db.execute(
        "UPDATE worker_state SET off_last_called_at = ? WHERE id = 1",
        ((fixed_now - timedelta(seconds=2)).isoformat(),)
    )
    db.commit()
    _seed(db)

    with patch("src.worker.main.get_connection", _make_get_connection(db)), \
         patch("src.worker.main.datetime") as mock_dt, \
         patch("src.worker.main.time.sleep") as mock_sleep, \
         patch("src.worker.off.lookup_barcode", return_value=_GOOD_OFF), \
         patch("src.worker.sainsburys.get_price", return_value=_GOOD_PRICE):
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        _poll_iteration(db)

    # At least one sleep of ~2s happened (the pacing sleep before the OFF call).
    assert any(abs(c.args[0] - 2.0) < 0.05 for c in mock_sleep.call_args_list)


# ---------------------------------------------------------------------------
# Poll query priority
# ---------------------------------------------------------------------------

def test_poll1_info_pending_is_processed_first(db):
    _seed(db, info_status="pending", price_status="pending")

    with patch("src.worker.main.get_connection", _make_get_connection(db)), \
         patch("src.worker.off.lookup_barcode", return_value=_GOOD_OFF), \
         patch("src.worker.sainsburys.get_price", return_value=_GOOD_PRICE):
        did_work = _poll_iteration(db)

    assert did_work is True
    row = db.execute(
        "SELECT info_status, price_status FROM session_items WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert row["info_status"] == "resolved"
    assert row["price_status"] == "resolved"


def test_poll_queries_still_use_partial_indexes(db):
    # The re-key (adding retailer_id to the projection/WHERE) must not disqualify the two partial
    # indexes backing the polls. Confirm via EXPLAIN QUERY PLAN.
    poll1_plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT barcode, session_id, retailer_id FROM session_items "
        "WHERE info_status = 'pending' ORDER BY first_scanned_at ASC LIMIT 1"
    ).fetchall()
    assert any("idx_session_items_info_pending" in row["detail"] for row in poll1_plan)

    poll2_plan = db.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT si.barcode, si.session_id, si.retailer_id, pv.name, pv.brand, pv.product_quantity "
        "FROM session_items si "
        "LEFT JOIN product_variants pv ON pv.barcode = si.barcode AND pv.retailer_id = si.retailer_id "
        "WHERE si.info_status = 'resolved' AND si.price_status = 'pending' "
        "ORDER BY si.first_scanned_at ASC LIMIT 1"
    ).fetchall()
    assert any("idx_session_items_price_pending" in row["detail"] for row in poll2_plan)


def test_poll2_only_runs_when_poll1_empty(db):
    _seed(db, info_status="resolved", price_status="pending")
    _seed_variant(db, name="Baked Beans", brand="Heinz", product_quantity="415g", lookup_status="resolved")

    with patch("src.worker.main.get_connection", _make_get_connection(db)), \
         patch("src.worker.sainsburys.get_price", return_value=_GOOD_PRICE) as mock_price:
        did_work = _poll_iteration(db)

    assert did_work is True
    mock_price.assert_called_once()
    row = db.execute("SELECT price_status FROM session_items WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert row["price_status"] == "resolved"


# ---------------------------------------------------------------------------
# R3: null-name poll-2 guard
# ---------------------------------------------------------------------------

def test_phase2_not_possible_stamps_and_writes_no_prices_row(db):
    _seed_variant(db, lookup_status='failed', lookup_failure_count=1)  # null-data row
    _seed(db, info_status="resolved", price_status="pending")

    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        rowcount = _phase2_not_possible(_BARCODE, _SESSION_ID, _RETAILER_ID)

    assert rowcount == 1
    si = db.execute("SELECT price_status FROM session_items WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert si["price_status"] == "not_possible"
    assert db.execute("SELECT COUNT(*) AS c FROM prices WHERE barcode = ?", (_BARCODE,)).fetchone()["c"] == 0


def test_poll2_null_name_skips_scraper_and_stamps_not_possible(db):
    # A re-scanned failed-OFF barcode carries a null-data variant row and is routed to poll-2.
    # The worker must NOT call Sainsbury's (no keyword) and must stamp not_possible.
    _seed_variant(db, lookup_status='failed', lookup_failure_count=1)  # null name
    _seed(db, info_status="resolved", price_status="pending")

    with patch("src.worker.main.get_connection", _make_get_connection(db)), \
         patch("src.worker.sainsburys.get_price") as mock_price:
        did_work = _poll_iteration(db)

    assert did_work is True
    mock_price.assert_not_called()
    si = db.execute("SELECT price_status FROM session_items WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert si["price_status"] == "not_possible"


def test_failed_off_then_rescan_reaches_confirmable(db):
    # End-to-end (no deadlock): scan -> OFF failure -> re-scan in a new session -> confirmable.
    # scan.py's real has_info stamping is exercised via _do_scan.
    from src.api.routers import scan as scan_router

    get_conn = _make_get_connection(db)

    # Session 1: scan (no PV row yet -> info pending), then OFF fails.
    db.execute("INSERT INTO sessions (id, type, started_at) VALUES (1, 'in', '2026-05-27T10:00:00')")
    db.commit()
    with patch("src.api.routers.scan.get_connection", get_conn), \
         patch("src.api.routers.scan.datetime") as scan_dt:
        scan_dt.now.return_value = datetime(2026, 7, 21, 10, 0, 0, tzinfo=UTC)
        r1 = scan_router._do_scan(_BARCODE, _RETAILER_ID)
    assert r1["info_status"] == "pending"

    with patch("src.worker.main.get_connection", get_conn), \
         patch("src.worker.off.lookup_barcode", return_value=None), \
         patch("src.worker.sainsburys.get_price") as mock_price:
        _poll_iteration(db)
    mock_price.assert_not_called()

    # Session 1 is now confirmable (no pending), and OFF left a failed null-data variant row.
    pending1 = db.execute(
        "SELECT COUNT(*) AS c FROM session_items WHERE session_id = 1 "
        "AND (info_status = 'pending' OR price_status = 'pending')"
    ).fetchone()["c"]
    assert pending1 == 0

    # Session 1 confirmed/cleared; the durable failed variant row persists as cache.
    db.execute("DELETE FROM sessions WHERE id = 1")
    db.commit()

    # Session 2: re-scan the same barcode. scan.py sees the variant row exists (has_info=True)
    # and stamps info='resolved', price='pending' -> routed to poll-2.
    db.execute("INSERT INTO sessions (id, type, started_at) VALUES (2, 'in', '2026-05-27T11:00:00')")
    db.commit()
    with patch("src.api.routers.scan.get_connection", get_conn), \
         patch("src.api.routers.scan.datetime") as scan_dt:
        scan_dt.now.return_value = datetime(2026, 7, 21, 11, 0, 0, tzinfo=UTC)
        r2 = scan_router._do_scan(_BARCODE, _RETAILER_ID)
    assert r2["info_status"] == "resolved"
    assert r2["price_status"] == "pending"

    with patch("src.worker.main.get_connection", get_conn), \
         patch("src.worker.sainsburys.get_price") as mock_price2:
        _poll_iteration(db)
    mock_price2.assert_not_called()  # null name -> no scraper call

    # Session 2 reaches a confirmable state — nothing stuck pending.
    pending2 = db.execute(
        "SELECT COUNT(*) AS c FROM session_items WHERE session_id = 2 "
        "AND (info_status = 'pending' OR price_status = 'pending')"
    ).fetchone()["c"]
    assert pending2 == 0


# ---------------------------------------------------------------------------
# Poll loop resilience (Issue 1 fix)
# ---------------------------------------------------------------------------

class _LoopBreak(BaseException):
    """Not an Exception, so _poll_forever's `except Exception` won't swallow it —
    used to break out of the otherwise-infinite loop once the test has seen enough."""


def test_poll_forever_survives_unexpected_exception():
    calls = []

    def fake_iteration(db):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        raise _LoopBreak

    with patch("src.worker.main._poll_iteration", side_effect=fake_iteration), \
         patch("src.worker.main.time.sleep") as mock_sleep, \
         patch("src.worker.main.logger") as mock_logger:
        with pytest.raises(_LoopBreak):
            _poll_forever(MagicMock())

    assert len(calls) == 2
    mock_logger.exception.assert_called_once()
    mock_sleep.assert_called_once()
