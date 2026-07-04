"""Tests for src/api/routers/ws.py and src/api/main.py — WebSocket and poll loop."""

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from src.api.dependencies import get_db, verify_api_key
from src.api.main import _compute_poll_updates, app
from src.api.routers.ws import _status_to_wire, build_payload, manager

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, name TEXT, brand TEXT, product_quantity TEXT, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit', product_url TEXT NULL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 0, minimum_quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE sessions (id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('in', 'out')), started_at TEXT NOT NULL, recovered_at TEXT);
CREATE TABLE session_items (session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL, delta INTEGER NOT NULL CHECK(delta >= 0), info_status TEXT NOT NULL DEFAULT 'pending' CHECK(info_status IN ('pending', 'resolved', 'failed')), price_status TEXT NOT NULL DEFAULT 'pending' CHECK(price_status IN ('pending', 'resolved', 'failed', 'not_possible')), first_scanned_at TEXT NOT NULL, PRIMARY KEY (session_id, barcode, retailer_id));
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


@pytest.fixture(autouse=True)
def clear_manager():
    """Ensure a clean ConnectionManager between every test."""
    manager.active_connections.clear()
    yield
    manager.active_connections.clear()


def _create_session(db, session_type="in") -> int:
    db.execute(
        "INSERT INTO sessions (type, started_at) VALUES (?, '2026-05-27T10:00:00')",
        (session_type,),
    )
    db.commit()
    return db.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _make_row(barcode, info_status, price_status, session_delta=1, product_url=None):
    """Plain dict acting as a poll-query result row."""
    return {
        "barcode": barcode,
        "info_status": info_status,
        "price_status": price_status,
        "session_delta": session_delta,
        "name": "Test Product",
        "brand": "Test Brand",
        "product_quantity": "400g",
        "price_pence": 100,
        "product_url": product_url,
    }


# ── ConnectionManager ────────────────────────────────────────────────────────

def test_cm_connect_adds_client(client):
    with client.websocket_connect("/ws"):
        # Assert inside the with block — connection is removed on exit
        assert len(manager.active_connections) == 1


def test_cm_disconnect_removes_client(client):
    with client.websocket_connect("/ws"):
        pass
    # Assert after the with block exits — disconnect has fired
    assert len(manager.active_connections) == 0


def test_cm_broadcast_sends_to_live_clients():
    ws1, ws2, ws3 = AsyncMock(), AsyncMock(), AsyncMock()
    manager.active_connections = [ws1, ws2, ws3]
    asyncio.run(manager.broadcast("hello"))
    ws1.send_text.assert_called_once_with("hello")
    ws2.send_text.assert_called_once_with("hello")
    ws3.send_text.assert_called_once_with("hello")


def test_cm_broadcast_prunes_dead_client():
    live = AsyncMock()
    dead = AsyncMock()
    dead.send_text.side_effect = Exception("connection closed")
    manager.active_connections = [live, dead]
    asyncio.run(manager.broadcast("hello"))
    live.send_text.assert_called_once_with("hello")
    assert dead not in manager.active_connections
    assert live in manager.active_connections


# ── Scan endpoint broadcast ──────────────────────────────────────────────────

def test_scan_broadcasts_scan_payload(client, db):
    _create_session(db)
    db.execute("INSERT INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name) VALUES (?, ?, 'Beans')",
        (_BARCODE, _RETAILER_ID),
    )
    db.execute(
        "INSERT INTO prices (barcode, retailer_id, price_pence, price_type) VALUES (?, ?, 123, 'unit')",
        (_BARCODE, _RETAILER_ID),
    )
    db.commit()

    with client.websocket_connect("/ws") as ws:
        resp = client.post("/scan", json={"barcode": _BARCODE})
        assert resp.status_code == 200
        msg = ws.receive_json()

    assert msg["type"] == "scan"
    assert msg["barcode"] == _BARCODE
    assert msg["retailer"] == 1
    assert msg["session_delta"] == 1


def test_scan_no_broadcast_on_failure(client, db):
    with patch.object(manager, "broadcast", new_callable=AsyncMock) as mock_broadcast:
        resp = client.post("/scan", json={"barcode": _BARCODE})
        assert resp.status_code == 409
        mock_broadcast.assert_not_called()


# ── Poll loop logic (_compute_poll_updates) ──────────────────────────────────

def test_poll_emits_on_first_sighting():
    last_seen = {}
    rows = [_make_row(_BARCODE, "resolved", "resolved")]
    payloads = _compute_poll_updates(rows, last_seen, _RETAILER_ID)
    assert len(payloads) == 1
    assert last_seen[_BARCODE] == ("resolved", "resolved")
    msg = json.loads(payloads[0])
    assert msg["type"] == "resolution"
    assert msg["barcode"] == _BARCODE
    assert msg["retailer"] == 1


def test_poll_emits_on_status_change():
    last_seen = {_BARCODE: ("pending", "pending")}
    rows = [_make_row(_BARCODE, "resolved", "pending")]
    payloads = _compute_poll_updates(rows, last_seen, _RETAILER_ID)
    assert len(payloads) == 1
    assert last_seen[_BARCODE] == ("resolved", "pending")


def test_poll_silent_on_no_change():
    last_seen = {_BARCODE: ("resolved", "resolved")}
    rows = [_make_row(_BARCODE, "resolved", "resolved")]
    payloads = _compute_poll_updates(rows, last_seen, _RETAILER_ID)
    assert len(payloads) == 0
    assert last_seen[_BARCODE] == ("resolved", "resolved")


def test_poll_prunes_removed_barcode():
    last_seen = {_BARCODE: ("resolved", "resolved")}
    payloads = _compute_poll_updates([], last_seen, _RETAILER_ID)
    assert len(payloads) == 0
    assert last_seen == {}


def test_poll_emits_after_restart():
    last_seen = {}
    rows = [_make_row(_BARCODE, "resolved", "resolved")]
    payloads = _compute_poll_updates(rows, last_seen, _RETAILER_ID)
    assert len(payloads) == 1


# ── Status mapping ───────────────────────────────────────────────────────────

def test_status_mapping_pending_to_loading():
    assert _status_to_wire("pending") == "loading"


def test_status_mapping_resolved():
    assert _status_to_wire("resolved") == "resolved"


def test_status_mapping_failed():
    assert _status_to_wire("failed") == "failed"


def test_status_mapping_not_possible():
    assert _status_to_wire("not_possible") == "failed"


# ── WebSocket endpoint (end-to-end) ─────────────────────────────────────────

def test_ws_connect_and_receive(client, db):
    _create_session(db)
    db.execute("INSERT INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name) VALUES (?, ?, 'Beans')",
        (_BARCODE, _RETAILER_ID),
    )
    db.commit()

    with client.websocket_connect("/ws") as ws:
        resp = client.post("/scan", json={"barcode": _BARCODE})
        assert resp.status_code == 200
        msg = ws.receive_json()

    assert msg["type"] == "scan"


# ---------------------------------------------------------------------------
# Inventory quantity in scan broadcast
# ---------------------------------------------------------------------------

def test_scan_broadcast_includes_inventory_quantity(client, db):
    _create_session(db)
    db.execute("INSERT INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name) VALUES (?, ?, 'Beans')",
        (_BARCODE, _RETAILER_ID),
    )
    db.execute(
        "INSERT INTO prices (barcode, retailer_id, price_pence, price_type) VALUES (?, ?, 123, 'unit')",
        (_BARCODE, _RETAILER_ID),
    )
    db.execute("INSERT INTO inventory (barcode, retailer_id, quantity) VALUES (?, ?, 2)", (_BARCODE, _RETAILER_ID))
    db.commit()

    with client.websocket_connect("/ws") as ws:
        resp = client.post("/scan", json={"barcode": _BARCODE})
        assert resp.status_code == 200
        msg = ws.receive_json()

    assert msg["inventory_quantity"] == 2


def test_scan_broadcast_inventory_quantity_zero_for_unknown(client, db):
    _create_session(db)
    db.execute("INSERT INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.commit()
    # No inventory row

    with client.websocket_connect("/ws") as ws:
        resp = client.post("/scan", json={"barcode": _BARCODE})
        assert resp.status_code == 200
        msg = ws.receive_json()

    assert msg["inventory_quantity"] == 0


# ── off_url and price_url in WS payloads ────────────────────────────────────

_OFF_VIEW = "https://world.openfoodfacts.org/product/{}"
_OFF_ADD  = "https://world.openfoodfacts.org/cgi/product.pl?type=edit&code={}"


def test_build_payload_off_url_view_when_resolved():
    row = _make_row(_BARCODE, "resolved", "resolved")
    payload = build_payload("resolution", row, _RETAILER_ID)
    assert payload["off_url"] == _OFF_VIEW.format(_BARCODE)
    assert payload["retailer"] == 1


def test_build_payload_off_url_add_when_failed():
    row = _make_row(_BARCODE, "failed", "not_possible")
    payload = build_payload("resolution", row, _RETAILER_ID)
    assert payload["off_url"] == _OFF_ADD.format(_BARCODE)


def test_build_payload_price_url_none_when_absent():
    row = _make_row(_BARCODE, "resolved", "resolved", product_url=None)
    payload = build_payload("scan", row, _RETAILER_ID)
    assert payload["price_url"] is None


def test_build_payload_price_url_present_when_set():
    url = "https://www.sainsburys.co.uk/gol-ui/product/test"
    row = _make_row(_BARCODE, "resolved", "resolved", product_url=url)
    payload = build_payload("scan", row, _RETAILER_ID)
    assert payload["price_url"] == url


def test_scan_broadcast_includes_off_url(client, db):
    _create_session(db)
    db.execute("INSERT INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name) VALUES (?, ?, 'Beans')",
        (_BARCODE, _RETAILER_ID),
    )
    db.commit()

    with client.websocket_connect("/ws") as ws:
        client.post("/scan", json={"barcode": _BARCODE})
        msg = ws.receive_json()

    assert "off_url" in msg
    assert msg["off_url"] == _OFF_VIEW.format(_BARCODE)


def test_scan_broadcast_price_url_none_when_no_product_url(client, db):
    _create_session(db)
    db.execute("INSERT INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name) VALUES (?, ?, 'Beans')",
        (_BARCODE, _RETAILER_ID),
    )
    db.commit()

    with client.websocket_connect("/ws") as ws:
        client.post("/scan", json={"barcode": _BARCODE})
        msg = ws.receive_json()

    assert msg["price_url"] is None


# ── Item 25: broadcast() with zero clients ───────────────────────────────────

def test_cm_broadcast_zero_clients_no_error():
    manager.active_connections = []
    asyncio.run(manager.broadcast("hello"))
    assert manager.active_connections == []


# ── Item 29: poll emits resolution when price_status flips ──────────────────

def test_poll_emits_on_price_status_change():
    last_seen = {_BARCODE: ("resolved", "pending")}
    rows = [_make_row(_BARCODE, "resolved", "resolved")]
    payloads = _compute_poll_updates(rows, last_seen, _RETAILER_ID)
    assert len(payloads) == 1
    assert last_seen[_BARCODE] == ("resolved", "resolved")
    msg = json.loads(payloads[0])
    assert msg["type"] == "resolution"
    assert msg["barcode"] == _BARCODE
    assert msg["retailer"] == 1


# ── Item 55: scan broadcast includes price_url when product_url is set ───────

def test_scan_broadcast_includes_price_url_when_set(client, db):
    _create_session(db)
    url = "https://www.sainsburys.co.uk/gol-ui/product/test"
    db.execute("INSERT INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name) VALUES (?, ?, 'Beans')",
        (_BARCODE, _RETAILER_ID),
    )
    db.execute(
        "INSERT INTO prices (barcode, retailer_id, price_pence, price_type, product_url)"
        " VALUES (?, ?, 123, 'unit', ?)",
        (_BARCODE, _RETAILER_ID, url),
    )
    db.commit()

    with client.websocket_connect("/ws") as ws:
        client.post("/scan", json={"barcode": _BARCODE})
        msg = ws.receive_json()

    assert msg["price_url"] == url


# ── Item 56: build_payload("resolution", ...) price_url ─────────────────────

def test_build_payload_resolution_price_url_present_when_set():
    url = "https://www.sainsburys.co.uk/gol-ui/product/test"
    row = _make_row(_BARCODE, "resolved", "resolved", product_url=url)
    payload = build_payload("resolution", row, _RETAILER_ID)
    assert payload["price_url"] == url


def test_build_payload_resolution_price_url_none_when_absent():
    row = _make_row(_BARCODE, "resolved", "resolved", product_url=None)
    payload = build_payload("resolution", row, _RETAILER_ID)
    assert payload["price_url"] is None
