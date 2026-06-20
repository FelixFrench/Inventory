"""Tests for src/api/routers/scan.py — session-aware scan endpoint behaviour."""

import sqlite3
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from src.api.dependencies import get_db, verify_api_key
from src.api.main import app

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, name TEXT, brand TEXT, weight_g REAL, product_quantity TEXT, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit', product_url TEXT NULL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT PRIMARY KEY, quantity INTEGER NOT NULL DEFAULT 0, minimum_quantity INTEGER NOT NULL DEFAULT 0);
CREATE TABLE sessions (id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('in', 'out')), started_at TEXT NOT NULL, recovered_at TEXT);
CREATE TABLE session_items (session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, barcode TEXT NOT NULL REFERENCES barcodes(barcode), delta INTEGER NOT NULL CHECK(delta >= 0), info_status TEXT NOT NULL DEFAULT 'pending' CHECK(info_status IN ('pending', 'resolved', 'failed')), price_status TEXT NOT NULL DEFAULT 'pending' CHECK(price_status IN ('pending', 'resolved', 'failed', 'not_possible')), first_scanned_at TEXT NOT NULL, PRIMARY KEY (session_id, barcode));
CREATE TABLE worker_state (id INTEGER PRIMARY KEY CHECK(id = 1), off_last_called_at TEXT NOT NULL);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO worker_state (id, off_last_called_at) VALUES (1, '1970-01-01T00:00:00');
"""

_RETAILER_ID = 1
_BARCODE = "5014788110140"


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
def db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


@pytest.fixture
def client(db):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_api_key] = lambda: None
    app.state.sainsburys_retailer_id = _RETAILER_ID
    with patch("src.api.routers.scan.get_connection", lambda: _NocloseConn(db)):
        yield TestClient(app)
    app.dependency_overrides.clear()


def _create_session(db, session_type="in") -> int:
    db.execute(
        "INSERT INTO sessions (type, started_at) VALUES (?, '2026-05-27T10:00:00')",
        (session_type,)
    )
    db.commit()
    return db.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()["id"]


# ---------------------------------------------------------------------------
# Scan cases
# ---------------------------------------------------------------------------

def test_scan_unknown_barcode_creates_pending_item(client, db):
    session_id = _create_session(db)
    resp = client.post("/scan", json={"barcode": _BARCODE})
    assert resp.status_code == 200
    data = resp.json()
    assert data["barcode"] == _BARCODE
    assert data["session_delta"] == 1

    row = db.execute(
        "SELECT info_status, price_status, delta FROM session_items "
        "WHERE session_id = ? AND barcode = ?",
        (session_id, _BARCODE)
    ).fetchone()
    assert row is not None
    assert row["info_status"] == "pending"
    assert row["price_status"] == "pending"
    assert row["delta"] == 1

    bc = db.execute("SELECT barcode FROM barcodes WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert bc is not None


def test_scan_known_barcode_fully_resolved(client, db):
    session_id = _create_session(db)
    db.execute("INSERT INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name) VALUES (?, ?, 'Beans')",
        (_BARCODE, _RETAILER_ID)
    )
    db.execute(
        "INSERT INTO prices (barcode, retailer_id, price_pence, price_type) VALUES (?, ?, 123, 'unit')",
        (_BARCODE, _RETAILER_ID)
    )
    db.commit()

    resp = client.post("/scan", json={"barcode": _BARCODE})
    assert resp.status_code == 200

    row = db.execute(
        "SELECT info_status, price_status FROM session_items WHERE session_id = ? AND barcode = ?",
        (session_id, _BARCODE)
    ).fetchone()
    assert row["info_status"] == "resolved"
    assert row["price_status"] == "resolved"


def test_scan_known_barcode_no_price(client, db):
    session_id = _create_session(db)
    db.execute("INSERT INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name) VALUES (?, ?, 'Beans')",
        (_BARCODE, _RETAILER_ID)
    )
    db.commit()

    resp = client.post("/scan", json={"barcode": _BARCODE})
    assert resp.status_code == 200

    row = db.execute(
        "SELECT info_status, price_status FROM session_items WHERE session_id = ? AND barcode = ?",
        (session_id, _BARCODE)
    ).fetchone()
    assert row["info_status"] == "resolved"
    assert row["price_status"] == "pending"


def test_scan_no_active_session(client):
    resp = client.post("/scan", json={"barcode": _BARCODE})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "no_active_session"


# ---------------------------------------------------------------------------
# Repeat scan
# ---------------------------------------------------------------------------

def test_scan_repeat_increments_delta(client, db):
    _create_session(db)
    client.post("/scan", json={"barcode": _BARCODE})
    resp = client.post("/scan", json={"barcode": _BARCODE})
    assert resp.status_code == 200
    assert resp.json()["session_delta"] == 2

    row = db.execute(
        "SELECT delta FROM session_items WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert row["delta"] == 2

    count = db.execute(
        "SELECT COUNT(*) FROM session_items WHERE barcode = ?", (_BARCODE,)
    ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_scan_operational_error_returns_503(client):
    with patch("src.api.routers.scan.get_connection",
               side_effect=sqlite3.OperationalError("database is locked")):
        resp = client.post("/scan", json={"barcode": _BARCODE})

    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "1"
