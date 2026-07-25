"""1b composite re-key: the (barcode, retailer_id) key carried end-to-end.

Every other API test file runs with a single retailer, so nothing today distinguishes
``ON CONFLICT(session_id, barcode, retailer_id)`` from ``ON CONFLICT(session_id, barcode)``, or the
confirm flush's ``ON CONFLICT(barcode, retailer_id)`` from ``ON CONFLICT(barcode)``. These tests put
the SAME barcode under TWO retailers and assert the two rows move independently.

The fixture DB is built by running the real Alembic chain to head rather than from an inline
SCHEMA literal: the composite primary keys then come from production DDL, so the tests cannot be
silently disarmed by a stale hand-maintained schema copy. It also leaves every other test file's
shared SCHEMA constant untouched.
"""

import gc
import os
import shutil
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from src.api.dependencies import get_db, get_retailer_id, verify_api_key
from src.api.main import app
from src.api.routers.scan import _do_scan

_BARCODE = "5014788110140"
_R1 = 1  # Sainsbury's, seeded by initial_schema
_R2 = 2  # a second retailer, added by the fixture
_SESSION_ID = 1


def _safe_unlink(db_path):
    """Best-effort temp-DB cleanup (mirrors tests/db/test_migrations.py).

    On Windows a SQLite/WAL handle can linger briefly after the last connection closes,
    transiently locking the file; force a GC and retry, then give up silently.
    """
    for _ in range(20):
        try:
            os.unlink(db_path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            gc.collect()
            time.sleep(0.1)


@pytest.fixture(scope="module")
def head_db_template(tmp_path_factory):
    """Run the Alembic chain to head once; later tests copy the resulting file."""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path_factory.mktemp("rekey") / "head.db"
    alembic_cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(alembic_cfg, "head")

    # A second retailer, so a composite key has something to be composite about.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO retailers (id, name, scraper_class) VALUES (?, 'Testco', 'TestcoProvider')",
        (_R2,),
    )
    conn.commit()
    conn.close()

    yield db_path
    _safe_unlink(str(db_path))


@pytest.fixture
def db(head_db_template, tmp_path):
    path = tmp_path / "test.db"
    shutil.copyfile(head_db_template, path)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()
    _safe_unlink(str(path))


class _NocloseConn:
    """Wraps a sqlite3.Connection but ignores close() to protect the test fixture."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, *a, **kw):
        return self._conn.execute(*a, **kw)

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


@pytest.fixture
def client(db):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_retailer_id] = lambda: _R1
    with patch("src.api.routers.session.get_connection", lambda: _NocloseConn(db)):
        yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def scan_conn(db):
    """Patch the scan router's connection factory and silence the WS broadcast."""
    with patch("src.api.routers.scan.get_connection", lambda: _NocloseConn(db)), \
         patch("src.api.routers.scan.manager.broadcast", new_callable=AsyncMock):
        yield db


def _open_session(db, session_type="in"):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute(
        "INSERT INTO sessions (id, type, started_at) VALUES (?, ?, '2026-07-25T10:00:00')",
        (_SESSION_ID, session_type),
    )
    db.commit()


def _seed_item(db, retailer_id, delta, status="resolved"):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute(
        "INSERT INTO session_items "
        "(session_id, barcode, retailer_id, delta, info_status, price_status, first_scanned_at) "
        "VALUES (?, ?, ?, ?, ?, ?, '2026-07-25T10:00:00')",
        (_SESSION_ID, _BARCODE, retailer_id, delta, status, status),
    )
    db.commit()


def _seed_inventory(db, retailer_id, quantity):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute(
        "INSERT INTO inventory (barcode, retailer_id, quantity) VALUES (?, ?, ?)",
        (_BARCODE, retailer_id, quantity),
    )
    db.commit()


def _deltas(db):
    return {
        r["retailer_id"]: r["delta"]
        for r in db.execute(
            "SELECT retailer_id, delta FROM session_items WHERE barcode = ?", (_BARCODE,)
        ).fetchall()
    }


def _quantities(db):
    return {
        r["retailer_id"]: r["quantity"]
        for r in db.execute(
            "SELECT retailer_id, quantity FROM inventory WHERE barcode = ?", (_BARCODE,)
        ).fetchall()
    }


# ---------------------------------------------------------------------------
# Sanity: the fixture really is composite (a stale schema would disarm everything below)
# ---------------------------------------------------------------------------

def test_fixture_schema_is_actually_composite(db):
    def pk(table):
        rows = db.execute(f"PRAGMA table_info('{table}')").fetchall()
        return [r["name"] for r in sorted((r for r in rows if r["pk"]), key=lambda r: r["pk"])]

    assert pk("inventory") == ["barcode", "retailer_id"]
    assert pk("session_items") == ["session_id", "barcode", "retailer_id"]
    assert db.execute("SELECT COUNT(*) FROM retailers").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# scan.py: ON CONFLICT(session_id, barcode, retailer_id)
# ---------------------------------------------------------------------------

def test_scan_same_barcode_two_retailers_creates_two_rows(scan_conn):
    """A barcode-only conflict target would collapse these into one row with delta 2."""
    db = scan_conn
    _open_session(db)

    first = _do_scan(_BARCODE, _R1)
    second = _do_scan(_BARCODE, _R2)

    assert first["in_session"] is True and first["session_delta"] == 1
    assert second["in_session"] is True and second["session_delta"] == 1
    assert _deltas(db) == {_R1: 1, _R2: 1}


def test_scan_rescan_increments_only_the_scanned_retailer(scan_conn):
    """The DO UPDATE arm must be reached for the matching retailer and only that one."""
    db = scan_conn
    _open_session(db)

    _do_scan(_BARCODE, _R1)
    _do_scan(_BARCODE, _R2)
    third = _do_scan(_BARCODE, _R2)

    assert third["session_delta"] == 2
    assert _deltas(db) == {_R1: 1, _R2: 2}


# ---------------------------------------------------------------------------
# Confirm flush: ON CONFLICT(barcode, retailer_id)
# ---------------------------------------------------------------------------

def test_confirm_in_flush_is_retailer_scoped(client, db):
    """Retailer 1 has stock (conflict -> DO UPDATE); retailer 2 has none (fresh INSERT)."""
    _open_session(db, "in")
    _seed_inventory(db, _R1, 7)
    _seed_item(db, _R1, 2)
    _seed_item(db, _R2, 5)

    resp = client.post("/session/confirm")

    assert resp.status_code == 200
    assert resp.json()["applied_items"] == 2
    assert _quantities(db) == {_R1: 9, _R2: 5}


def test_confirm_out_flush_is_retailer_scoped(client, db):
    _open_session(db, "out")
    _seed_inventory(db, _R1, 7)
    _seed_inventory(db, _R2, 5)
    _seed_item(db, _R1, 2)
    _seed_item(db, _R2, 5)

    resp = client.post("/session/confirm")

    assert resp.status_code == 200
    assert _quantities(db) == {_R1: 5, _R2: 0}


# ---------------------------------------------------------------------------
# would_go_negative: the composite half of the LEFT JOIN
# ---------------------------------------------------------------------------

def test_would_go_negative_does_not_borrow_another_retailers_stock(client, db):
    """Retailer 1 holds 10; retailer 2 holds nothing. Only the retailer-2 row is negative.

    A join on barcode alone would see quantity 10 for both rows and let the confirm through.
    Note the 409 payload carries no retailer_id (current behaviour, documented not asserted as
    desirable) — so the discriminating assertion is that exactly ONE item is reported with
    current_quantity 0.
    """
    _open_session(db, "out")
    _seed_inventory(db, _R1, 10)
    _seed_item(db, _R1, 1)
    _seed_item(db, _R2, 1)

    resp = client.post("/session/confirm")

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "would_go_negative"
    assert detail["items"] == [
        {"barcode": _BARCODE, "current_quantity": 0, "delta": 1}
    ]
    # Nothing applied, session still open.
    assert _quantities(db) == {_R1: 10}
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_confirm_out_succeeds_once_each_retailer_has_its_own_stock(client, db):
    """The positive control for the test above: give retailer 2 its own row and it passes."""
    _open_session(db, "out")
    _seed_inventory(db, _R1, 10)
    _seed_inventory(db, _R2, 1)
    _seed_item(db, _R1, 1)
    _seed_item(db, _R2, 1)

    resp = client.post("/session/confirm")

    assert resp.status_code == 200
    assert _quantities(db) == {_R1: 9, _R2: 0}
