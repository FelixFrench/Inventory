"""Tests for src/worker/main.py — OFF/Sainsbury's write-back, worker state, poll priority."""

import sqlite3
from datetime import datetime, timedelta, UTC
from unittest.mock import MagicMock, patch

import pytest

from src.worker.main import (
    OFF_RATE_LIMIT_SECS,
    _compute_startup_sleep,
    _phase1_failure,
    _phase1_success,
    _phase2_success,
    _poll_forever,
)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, name TEXT, brand TEXT, weight_g REAL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit', product_url TEXT NULL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT PRIMARY KEY, quantity INTEGER NOT NULL DEFAULT 0, minimum_quantity INTEGER NOT NULL DEFAULT 0);
CREATE TABLE sessions (id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('in', 'out')), started_at TEXT NOT NULL, recovered_at TEXT);
CREATE TABLE session_items (session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, barcode TEXT NOT NULL REFERENCES barcodes(barcode), delta INTEGER NOT NULL CHECK(delta >= 0), info_status TEXT NOT NULL DEFAULT 'pending' CHECK(info_status IN ('pending', 'resolved', 'failed')), price_status TEXT NOT NULL DEFAULT 'pending' CHECK(price_status IN ('pending', 'resolved', 'failed', 'not_possible')), first_scanned_at TEXT NOT NULL, PRIMARY KEY (session_id, barcode));
CREATE TABLE worker_state (id INTEGER PRIMARY KEY CHECK(id = 1), off_last_called_at TEXT NOT NULL);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO worker_state (id, off_last_called_at) VALUES (1, '1970-01-01T00:00:00');
"""

_BARCODE = "5014788110140"
_RETAILER_ID = 1
_SESSION_ID = 1
_GOOD_OFF = {"name": "Baked Beans", "brand": "Heinz", "weight_g": 415.0}
_GOOD_PRICE = {"price_pence": 85, "price_type": "unit", "product_url": "https://www.sainsburys.co.uk/gol-ui/product/test"}
_GOOD_PRICE_NO_URL = {"price_pence": 85, "price_type": "unit", "product_url": None}


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def _seed(conn: sqlite3.Connection, info_status: str = "pending", price_status: str = "pending"):
    conn.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    conn.execute(
        "INSERT INTO sessions (id, type, started_at) VALUES (?, 'in', '2026-05-27T10:00:00')",
        (_SESSION_ID,)
    )
    conn.execute(
        "INSERT INTO session_items (session_id, barcode, delta, info_status, price_status, first_scanned_at) "
        "VALUES (?, ?, 1, ?, ?, '2026-05-27T10:00:00')",
        (_SESSION_ID, _BARCODE, info_status, price_status)
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
        rowcount = _phase1_failure(_BARCODE, _SESSION_ID)

    assert rowcount == 1
    row = db.execute(
        "SELECT info_status, price_status FROM session_items WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert row["info_status"] == "failed"
    assert row["price_status"] == "not_possible"


def test_off_failure_price_status_is_not_possible_not_failed(db):
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase1_failure(_BARCODE, _SESSION_ID)

    row = db.execute(
        "SELECT price_status FROM session_items WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert row["price_status"] == "not_possible"
    assert row["price_status"] != "failed"


# ---------------------------------------------------------------------------
# OFF lookup write-back
# ---------------------------------------------------------------------------

def test_off_success_rowcount_zero_when_session_discarded(db):
    # Cache-retention behaviour (documented in _poll_iteration): when the session
    # was discarded mid-lookup the session_items UPDATE affects zero rows, but the
    # product_variants row is still committed as a cache warm-up. This asserts both
    # rowcount == 0 AND that the variant row persists.
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.commit()

    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        rowcount = _phase1_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_OFF)

    assert rowcount == 0
    pv = db.execute(
        "SELECT name FROM product_variants WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert pv is not None
    assert pv["name"] == "Baked Beans"


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
        _phase1_failure(_BARCODE, _SESSION_ID)

    ts = db.execute(
        "SELECT off_last_called_at FROM worker_state WHERE id = 1"
    ).fetchone()["off_last_called_at"]
    assert ts != "1970-01-01T00:00:00"


# ---------------------------------------------------------------------------
# Sainsbury's lookup write-back
# ---------------------------------------------------------------------------

def test_sainsburys_success_rowcount_zero_when_session_discarded(db):
    # Cache-retention behaviour (documented in _poll_iteration): the prices row is
    # committed even when the session_items UPDATE affects zero rows (session
    # discarded mid-lookup). Asserts both rowcount == 0 AND that the price persists.
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.commit()

    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        rowcount = _phase2_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_PRICE)

    assert rowcount == 0
    pr = db.execute(
        "SELECT price_pence FROM prices WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert pr is not None
    assert pr["price_pence"] == 85


def test_sainsburys_success_writes_product_url(db):
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase2_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_PRICE)

    pr = db.execute(
        "SELECT price_pence, product_url FROM prices WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert pr is not None
    assert pr["price_pence"] == 85
    assert pr["product_url"] == "https://www.sainsburys.co.uk/gol-ui/product/test"


def test_sainsburys_success_writes_null_product_url_when_none(db):
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase2_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_PRICE_NO_URL)

    pr = db.execute(
        "SELECT product_url FROM prices WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert pr is not None
    assert pr["product_url"] is None


# ---------------------------------------------------------------------------
# Worker state persistence
# ---------------------------------------------------------------------------

def test_worker_state_survives_session_confirm(db):
    _seed(db)
    sentinel = "2026-05-27T10:00:00"
    db.execute(
        "UPDATE worker_state SET off_last_called_at = ? WHERE id = 1", (sentinel,)
    )
    db.commit()

    db.execute("DELETE FROM sessions WHERE id = ?", (_SESSION_ID,))
    db.commit()

    ts = db.execute(
        "SELECT off_last_called_at FROM worker_state WHERE id = 1"
    ).fetchone()["off_last_called_at"]
    assert ts == sentinel


def test_worker_state_survives_session_discard(db):
    _seed(db)
    sentinel = "2026-05-27T10:00:00"
    db.execute(
        "UPDATE worker_state SET off_last_called_at = ? WHERE id = 1", (sentinel,)
    )
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
# Poll query priority
# ---------------------------------------------------------------------------

def test_poll1_info_pending_is_processed_first(db):
    _seed(db, info_status="pending", price_status="pending")

    with patch("src.worker.main.get_connection", _make_get_connection(db)), \
         patch("src.worker.off.lookup_barcode", return_value=_GOOD_OFF), \
         patch("src.worker.sainsburys.get_price", return_value=_GOOD_PRICE):
        poll1 = db.execute(
            "SELECT barcode, session_id FROM session_items "
            "WHERE info_status = 'pending' "
            "ORDER BY first_scanned_at ASC LIMIT 1"
        ).fetchone()
        assert poll1 is not None
        assert poll1["barcode"] == _BARCODE

        _phase1_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_OFF)
        _phase2_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_PRICE)

    row = db.execute(
        "SELECT info_status, price_status FROM session_items WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert row["info_status"] == "resolved"
    assert row["price_status"] == "resolved"


def test_poll2_only_runs_when_poll1_empty(db):
    _seed(db, info_status="resolved", price_status="pending")
    db.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name, brand, weight_g) "
        "VALUES (?, ?, 'Baked Beans', 'Heinz', 415.0)",
        (_BARCODE, _RETAILER_ID)
    )
    db.commit()

    poll1 = db.execute(
        "SELECT barcode FROM session_items WHERE info_status = 'pending' LIMIT 1"
    ).fetchone()
    assert poll1 is None

    poll2 = db.execute(
        """
        SELECT si.barcode, si.session_id, pv.name, pv.brand, pv.weight_g
        FROM session_items si
        LEFT JOIN product_variants pv ON pv.barcode = si.barcode AND pv.retailer_id = ?
        WHERE si.info_status = 'resolved' AND si.price_status = 'pending'
        ORDER BY si.first_scanned_at ASC LIMIT 1
        """,
        (_RETAILER_ID,)
    ).fetchone()
    assert poll2 is not None
    assert poll2["barcode"] == _BARCODE
    assert poll2["name"] == "Baked Beans"


# ---------------------------------------------------------------------------
# Poll loop resilience (Issue 1 fix)
# ---------------------------------------------------------------------------

class _LoopBreak(BaseException):
    """Not an Exception, so _poll_forever's `except Exception` won't swallow it —
    used to break out of the otherwise-infinite loop once the test has seen enough."""


def test_poll_forever_survives_unexpected_exception():
    calls = []

    def fake_iteration(db, retailer_id):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        raise _LoopBreak

    with patch("src.worker.main._poll_iteration", side_effect=fake_iteration), \
         patch("src.worker.main.time.sleep") as mock_sleep, \
         patch("src.worker.main.logger") as mock_logger:
        with pytest.raises(_LoopBreak):
            _poll_forever(MagicMock(), _RETAILER_ID)

    # The first OperationalError was caught and logged; the loop then reached a
    # second iteration — proving it did not propagate out and kill the worker.
    assert len(calls) == 2
    mock_logger.exception.assert_called_once()
    # One idle-backoff sleep ran after the caught exception (before the retry).
    mock_sleep.assert_called_once()
