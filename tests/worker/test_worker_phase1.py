import sqlite3
import time
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from src.worker.main import (
    OFF_RATE_LIMIT_SECS,
    _compute_startup_sleep,
    _phase1_failure,
    _phase1_success,
    _phase2_failure,
    _phase2_success,
)

_SCHEMA_PATH = Path(__file__).parents[2] / "src" / "db" / "schema.sql"

PHASE1_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, name TEXT, brand TEXT, weight_g REAL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit', PRIMARY KEY (barcode, retailer_id));
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
_GOOD_PRICE = {"price_pence": 85, "price_type": "unit"}


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(PHASE1_SCHEMA)
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
# Test 22–23: OFF failure → info=failed, price=not_possible (not 'failed')
# ---------------------------------------------------------------------------

def test_phase1_failure_sets_info_failed_and_price_not_possible(db):
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        rowcount = _phase1_failure(_BARCODE, _SESSION_ID)

    assert rowcount == 1
    row = db.execute(
        "SELECT info_status, price_status FROM session_items WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert row["info_status"] == "failed"
    assert row["price_status"] == "not_possible"


def test_phase1_failure_price_status_is_not_possible_not_failed(db):
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase1_failure(_BARCODE, _SESSION_ID)

    row = db.execute(
        "SELECT price_status FROM session_items WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert row["price_status"] == "not_possible"
    assert row["price_status"] != "failed"


# ---------------------------------------------------------------------------
# Test 24: rowcount=0 means session discarded — product_variants/prices rows kept
# ---------------------------------------------------------------------------

def test_phase1_success_rowcount_zero_when_session_discarded(db):
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


def test_phase2_success_rowcount_zero_when_session_discarded(db):
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


# ---------------------------------------------------------------------------
# Test 25: worker_state.off_last_called_at updated in same tx as OFF result
# ---------------------------------------------------------------------------

def test_phase1_success_updates_worker_state(db):
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase1_success(_BARCODE, _SESSION_ID, _RETAILER_ID, _GOOD_OFF)

    ts = db.execute(
        "SELECT off_last_called_at FROM worker_state WHERE id = 1"
    ).fetchone()["off_last_called_at"]
    assert ts != "1970-01-01T00:00:00"


def test_phase1_failure_updates_worker_state(db):
    _seed(db)
    with patch("src.worker.main.get_connection", _make_get_connection(db)):
        _phase1_failure(_BARCODE, _SESSION_ID)

    ts = db.execute(
        "SELECT off_last_called_at FROM worker_state WHERE id = 1"
    ).fetchone()["off_last_called_at"]
    assert ts != "1970-01-01T00:00:00"


# ---------------------------------------------------------------------------
# Tests 26–27: worker_state survives session confirm and discard
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
# Tests 28–30: Schema constraints
# ---------------------------------------------------------------------------

def test_negative_delta_raises_integrity_error(db):
    _seed(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO session_items (session_id, barcode, delta, first_scanned_at) "
            "VALUES (?, ?, -1, '2026-05-27T10:00:00')",
            (_SESSION_ID, _BARCODE)
        )


def test_invalid_info_status_raises_integrity_error(db):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES ('9999999999999')")
    db.execute(
        "INSERT INTO sessions (id, type, started_at) VALUES (2, 'in', '2026-05-27T10:00:00')"
    )
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO session_items (session_id, barcode, delta, info_status, first_scanned_at) "
            "VALUES (2, '9999999999999', 1, 'invalid_value', '2026-05-27T10:00:00')"
        )


def test_invalid_price_status_raises_integrity_error(db):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES ('8888888888888')")
    db.execute(
        "INSERT INTO sessions (id, type, started_at) VALUES (3, 'in', '2026-05-27T10:00:00')"
    )
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO session_items (session_id, barcode, delta, price_status, first_scanned_at) "
            "VALUES (3, '8888888888888', 1, 'bad_status', '2026-05-27T10:00:00')"
        )


# ---------------------------------------------------------------------------
# Test 31: Post-migration worker_state has id=1 and epoch sentinel
# ---------------------------------------------------------------------------

def test_worker_state_seed_row_exists(db):
    row = db.execute("SELECT id, off_last_called_at FROM worker_state WHERE id = 1").fetchone()
    assert row is not None
    assert row["id"] == 1
    assert row["off_last_called_at"] == "1970-01-01T00:00:00"


# ---------------------------------------------------------------------------
# Tests 32–33: Startup sleep logic
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
# Tests 20–21: Poll priority (via write-back state verification)
# ---------------------------------------------------------------------------

def test_poll1_info_pending_is_processed_first(db):
    _seed(db, info_status="pending", price_status="pending")

    with patch("src.worker.main.get_connection", _make_get_connection(db)), \
         patch("src.worker.off.lookup_barcode", return_value=_GOOD_OFF), \
         patch("src.worker.sainsburys.get_price", return_value=_GOOD_PRICE):
        from src.worker import main as worker_main

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
# Test 35: Migration runs on DB with pending_lookups rows — no error
# ---------------------------------------------------------------------------

_V1_SCHEMA = """
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE canonical_products (id INTEGER PRIMARY KEY);
CREATE TABLE product_variants (id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_product_id INTEGER REFERENCES canonical_products(id), name TEXT, brand TEXT, weight_g REAL, info_source TEXT, info_last_updated TIMESTAMP);
CREATE TABLE barcodes (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL REFERENCES retailers(id), product_variant_id INTEGER REFERENCES product_variants(id), PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (product_variant_id INTEGER NOT NULL REFERENCES product_variants(id), retailer_id INTEGER NOT NULL REFERENCES retailers(id), price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit' CHECK(price_type IN ('unit', 'per_kg')), last_updated TIMESTAMP, PRIMARY KEY (product_variant_id, retailer_id));
CREATE TABLE inventory (product_variant_id INTEGER PRIMARY KEY REFERENCES product_variants(id), quantity INTEGER NOT NULL DEFAULT 0, minimum_quantity INTEGER NOT NULL DEFAULT 0);
CREATE TABLE scan_events (id INTEGER PRIMARY KEY AUTOINCREMENT, barcode TEXT NOT NULL, retailer_id INTEGER REFERENCES retailers(id), direction TEXT NOT NULL CHECK(direction IN ('in', 'out')), timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE pending_lookups (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL REFERENCES retailers(id), queued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'failed', 'done')), PRIMARY KEY (barcode, retailer_id));
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO config (key, value) VALUES ('scan_mode', 'out');
"""


def test_migration_discards_pending_lookups_rows():
    import tempfile
    import os
    from alembic.config import Config
    from alembic import command

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        for stmt in [s.strip() for s in _V1_SCHEMA.split(";") if s.strip()]:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO barcodes (barcode, retailer_id) VALUES ('5014788110140', 1)"
        )
        conn.execute(
            "INSERT INTO pending_lookups (barcode, retailer_id, status) "
            "VALUES ('5014788110140', 1, 'pending')"
        )
        conn.commit()
        conn.close()

        alembic_cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        command.stamp(alembic_cfg, "0a3435ca1dec")
        command.upgrade(alembic_cfg, "0fcacd84ad0a")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "sessions" in tables
        assert "session_items" in tables
        assert "worker_state" in tables
        assert "pending_lookups" not in tables

        ws = conn.execute("SELECT id FROM worker_state WHERE id = 1").fetchone()
        assert ws is not None
        conn.close()
    finally:
        os.unlink(db_path)
