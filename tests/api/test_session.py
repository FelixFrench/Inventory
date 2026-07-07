"""Tests for src/api/routers/session.py — session lifecycle and item management."""

import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from src.api.dependencies import get_db, get_retailer_id, verify_api_key
from src.api.main import app

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, name TEXT, brand TEXT, product_quantity TEXT, minimum_quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit', product_url TEXT NULL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
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
    app.dependency_overrides[get_retailer_id] = lambda: _RETAILER_ID
    with patch("src.api.routers.session.get_connection", lambda: _NocloseConn(db)):
        yield TestClient(app)
    app.dependency_overrides.clear()


def _start_session(client, session_type="in"):
    resp = client.post("/session", json={"type": session_type})
    assert resp.status_code == 201
    return resp.json()["session"]["id"]


def _seed_item(db, barcode, session_id, delta=1,
               info_status="pending", price_status="pending",
               name=None, brand=None, product_quantity=None, price_pence=None, product_url=None):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (barcode,))
    db.execute(
        "INSERT INTO session_items (session_id, barcode, retailer_id, delta, info_status, price_status, first_scanned_at) "
        "VALUES (?, ?, ?, ?, ?, ?, '2026-05-27T10:00:00')",
        (session_id, barcode, _RETAILER_ID, delta, info_status, price_status)
    )
    if name is not None:
        db.execute(
            "INSERT OR IGNORE INTO product_variants (barcode, retailer_id, name, brand, product_quantity) "
            "VALUES (?, ?, ?, ?, ?)",
            (barcode, _RETAILER_ID, name, brand, product_quantity)
        )
    if price_pence is not None:
        db.execute(
            "INSERT OR IGNORE INTO prices (barcode, retailer_id, price_pence, price_type, product_url) "
            "VALUES (?, ?, ?, 'unit', ?)",
            (barcode, _RETAILER_ID, price_pence, product_url)
        )
    db.commit()


# ---------------------------------------------------------------------------
# Test 5: POST /session with type="in" → 201, session row in DB
# ---------------------------------------------------------------------------

def test_start_session_creates_row(client, db):
    resp = client.post("/session", json={"type": "in"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["session"]["type"] == "in"
    assert data["session"]["id"] is not None
    assert data["session"]["total_delta"] == 0
    assert data["session"]["items"] == []

    row = db.execute("SELECT type FROM sessions LIMIT 1").fetchone()
    assert row is not None
    assert row["type"] == "in"


# ---------------------------------------------------------------------------
# Test 6: POST /session when session already active → 409
# ---------------------------------------------------------------------------

def test_start_session_conflict(client, db):
    client.post("/session", json={"type": "in"})
    resp = client.post("/session", json={"type": "out"})
    assert resp.status_code == 409
    data = resp.json()
    assert data["detail"]["error"] == "session_already_active"
    assert "session" in data["detail"]


# ---------------------------------------------------------------------------
# Test 7: GET /session when no session → {"session": null}
# ---------------------------------------------------------------------------

def test_get_session_none(client):
    resp = client.get("/session")
    assert resp.status_code == 200
    assert resp.json() == {"session": None}


# ---------------------------------------------------------------------------
# Test 8: GET /session with items → full response with status mapping
# ---------------------------------------------------------------------------

def test_get_session_with_items(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=2,
               info_status="resolved", price_status="resolved",
               name="Baked Beans", brand="Heinz", product_quantity="415g", price_pence=123)

    resp = client.get("/session")
    assert resp.status_code == 200
    data = resp.json()["session"]
    assert data["total_delta"] == 2
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["barcode"] == _BARCODE
    assert item["delta"] == 2
    assert item["name"] == {"value": "Baked Beans", "status": "resolved"}
    assert item["brand"] == {"value": "Heinz", "status": "resolved"}
    assert item["quantity"] == {"value": "415g", "status": "resolved"}
    assert item["price"] == {"value": 1.23, "status": "resolved"}


def test_get_session_quantity_kilograms(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=1,
               info_status="resolved", price_status="resolved",
               name="Plain Flour", brand="Allinson", product_quantity="1.5kg", price_pence=200)

    resp = client.get("/session")
    assert resp.status_code == 200
    item = resp.json()["session"]["items"][0]
    assert item["quantity"] == {"value": "1.5kg", "status": "resolved"}


def test_get_session_status_loading(client, db):
    session_id = _start_session(client)
    _seed_item(db, _BARCODE, session_id, info_status="pending", price_status="pending")

    resp = client.get("/session")
    item = resp.json()["session"]["items"][0]
    assert item["name"]["status"] == "loading"
    assert item["price"]["status"] == "loading"


def test_get_session_status_not_possible_maps_to_failed(client, db):
    session_id = _start_session(client)
    _seed_item(db, _BARCODE, session_id, info_status="failed", price_status="not_possible")

    resp = client.get("/session")
    item = resp.json()["session"]["items"][0]
    assert item["name"]["status"] == "failed"
    assert item["price"]["status"] == "failed"


# ---------------------------------------------------------------------------
# Tests 9–10: Confirm session
# ---------------------------------------------------------------------------

def test_confirm_scan_in_increments_inventory(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=3,
               info_status="resolved", price_status="resolved")

    resp = client.post("/session/confirm")
    assert resp.status_code == 200
    data = resp.json()
    assert data["applied_items"] == 1
    assert data["session_id"] == session_id

    qty = db.execute("SELECT quantity FROM inventory WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert qty is not None
    assert qty["quantity"] == 3

    assert db.execute("SELECT id FROM sessions LIMIT 1").fetchone() is None
    assert db.execute("SELECT COUNT(*) FROM session_items").fetchone()[0] == 0


def test_confirm_scan_out_decrements_inventory(client, db):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute("INSERT INTO inventory (barcode, retailer_id, quantity) VALUES (?, ?, 10)", (_BARCODE, _RETAILER_ID))
    db.commit()
    session_id = _start_session(client, "out")
    _seed_item(db, _BARCODE, session_id, delta=3,
               info_status="resolved", price_status="resolved")

    resp = client.post("/session/confirm")
    assert resp.status_code == 200

    qty = db.execute("SELECT quantity FROM inventory WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert qty["quantity"] == 7


# ---------------------------------------------------------------------------
# Test 11–12: Confirm blocked by pending lookups
# ---------------------------------------------------------------------------

def test_confirm_blocked_by_info_pending(client, db):
    session_id = _start_session(client)
    _seed_item(db, _BARCODE, session_id, info_status="pending", price_status="pending")

    resp = client.post("/session/confirm")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "lookups_pending"
    assert resp.json()["detail"]["pending_count"] == 1


def test_confirm_blocked_by_price_pending(client, db):
    session_id = _start_session(client)
    _seed_item(db, _BARCODE, session_id, info_status="resolved", price_status="pending")

    resp = client.post("/session/confirm")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "lookups_pending"


def test_confirm_pending_check_inside_transaction(client, db):
    """Pending row present when transaction opens → 409, no inventory write, session preserved."""
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=3,
               info_status="pending", price_status="pending")

    resp = client.post("/session/confirm")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "lookups_pending"
    assert detail["pending_count"] == 1

    assert db.execute(
        "SELECT quantity FROM inventory WHERE barcode = ?", (_BARCODE,)
    ).fetchone() is None
    assert db.execute(
        "SELECT id FROM sessions WHERE id = ?", (session_id,)
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# Test 13: Scan-out would go negative
# ---------------------------------------------------------------------------

def test_confirm_scan_out_would_go_negative(client, db):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute("INSERT INTO inventory (barcode, retailer_id, quantity) VALUES (?, ?, 1)", (_BARCODE, _RETAILER_ID))
    db.commit()
    session_id = _start_session(client, "out")
    _seed_item(db, _BARCODE, session_id, delta=5,
               info_status="resolved", price_status="resolved")

    resp = client.post("/session/confirm")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "would_go_negative"
    assert len(detail["items"]) == 1
    assert detail["items"][0]["barcode"] == _BARCODE
    assert detail["items"][0]["current_quantity"] == 1
    assert detail["items"][0]["delta"] == 5

    # Inventory unchanged
    qty = db.execute("SELECT quantity FROM inventory WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert qty["quantity"] == 1


# ---------------------------------------------------------------------------
# Test 14: Scan-in does NOT check would-go-negative
# ---------------------------------------------------------------------------

def test_confirm_scan_in_never_checks_negative(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=100,
               info_status="resolved", price_status="resolved")

    resp = client.post("/session/confirm")
    assert resp.status_code == 200
    qty = db.execute("SELECT quantity FROM inventory WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert qty["quantity"] == 100


# ---------------------------------------------------------------------------
# Test 15: No active session → 409
# ---------------------------------------------------------------------------

def test_confirm_no_session(client):
    resp = client.post("/session/confirm")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "no_active_session"


# ---------------------------------------------------------------------------
# Test 16: delta=0 item skipped in flush, session still deleted
# ---------------------------------------------------------------------------

def test_confirm_zero_delta_item_skipped(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=0,
               info_status="resolved", price_status="resolved")

    resp = client.post("/session/confirm")
    assert resp.status_code == 200
    assert resp.json()["applied_items"] == 0
    assert db.execute("SELECT id FROM sessions LIMIT 1").fetchone() is None


# ---------------------------------------------------------------------------
# Tests 17–18: Discard
# ---------------------------------------------------------------------------

def test_discard_active_session(client, db):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute("INSERT INTO inventory (barcode, retailer_id, quantity) VALUES (?, ?, 5)", (_BARCODE, _RETAILER_ID))
    db.commit()
    session_id = _start_session(client)
    _seed_item(db, _BARCODE, session_id)

    resp = client.post("/session/discard")
    assert resp.status_code == 200
    assert resp.json()["discarded_session_id"] == session_id

    assert db.execute("SELECT id FROM sessions LIMIT 1").fetchone() is None
    assert db.execute("SELECT COUNT(*) FROM session_items").fetchone()[0] == 0
    qty = db.execute("SELECT quantity FROM inventory WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert qty["quantity"] == 5  # unchanged


def test_discard_no_session(client):
    resp = client.post("/session/discard")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "no_active_session"


# ---------------------------------------------------------------------------
# Test 19: Discard mid-resolution — session gone, data rows kept
# ---------------------------------------------------------------------------

def test_discard_then_worker_writeback_keeps_data_rows(client, db):
    from src.worker.main import _phase1_success

    session_id = _start_session(client)
    _seed_item(db, _BARCODE, session_id, info_status="pending", price_status="pending")

    client.post("/session/discard")
    assert db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone() is None

    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.commit()

    result = {"name": "Baked Beans", "brand": "Heinz", "product_quantity": "415g"}

    with patch("src.worker.main.get_connection", MagicMock(return_value=_NocloseConn(db))):
        rowcount = _phase1_success(_BARCODE, session_id, _RETAILER_ID, result)

    assert rowcount == 0
    pv = db.execute(
        "SELECT name FROM product_variants WHERE barcode = ?", (_BARCODE,)
    ).fetchone()
    assert pv is not None
    assert pv["name"] == "Baked Beans"


_BARCODE2 = "5000112548167"

# ---------------------------------------------------------------------------
# PUT /session/items/{barcode}
# ---------------------------------------------------------------------------

def test_put_delta_updates_row(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=1,
               info_status="resolved", price_status="resolved")

    with patch("src.api.routers.session.manager") as mock_mgr:
        mock_mgr.broadcast = AsyncMock()
        resp = client.put(f"/session/items/{_BARCODE}", json={"delta": 4})

    assert resp.status_code == 200
    data = resp.json()
    assert data["barcode"] == _BARCODE
    assert data["delta"] == 4
    row = db.execute(
        "SELECT delta FROM session_items WHERE session_id = ? AND barcode = ?",
        (session_id, _BARCODE)
    ).fetchone()
    assert row["delta"] == 4


def test_put_delta_zero_allowed(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=3,
               info_status="resolved", price_status="resolved")

    with patch("src.api.routers.session.manager") as mock_mgr:
        mock_mgr.broadcast = AsyncMock()
        resp = client.put(f"/session/items/{_BARCODE}", json={"delta": 0})

    assert resp.status_code == 200
    assert resp.json()["delta"] == 0
    row = db.execute(
        "SELECT delta FROM session_items WHERE session_id = ? AND barcode = ?",
        (session_id, _BARCODE)
    ).fetchone()
    assert row is not None
    assert row["delta"] == 0


def test_put_delta_negative_rejected(client, db):
    """A negative delta is rejected at the schema layer (Field(ge=0) → 422)."""
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=2,
               info_status="resolved", price_status="resolved")

    resp = client.put(f"/session/items/{_BARCODE}", json={"delta": -1})
    assert resp.status_code == 422


def test_put_delta_barcode_not_in_session(client, db):
    _start_session(client, "in")
    # _BARCODE not seeded into session_items

    with patch("src.api.routers.session.manager") as mock_mgr:
        mock_mgr.broadcast = AsyncMock()
        resp = client.put(f"/session/items/{_BARCODE}", json={"delta": 2})

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "item_not_found"
    mock_mgr.broadcast.assert_not_called()


def test_put_delta_no_active_session(client, db):
    with patch("src.api.routers.session.manager") as mock_mgr:
        mock_mgr.broadcast = AsyncMock()
        resp = client.put(f"/session/items/{_BARCODE}", json={"delta": 2})

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "no_active_session"
    mock_mgr.broadcast.assert_not_called()


def test_put_delta_session_total_correct(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=3,
               info_status="resolved", price_status="resolved")
    _seed_item(db, _BARCODE2, session_id, delta=2,
               info_status="resolved", price_status="resolved")

    with patch("src.api.routers.session.manager") as mock_mgr:
        mock_mgr.broadcast = AsyncMock()
        resp = client.put(f"/session/items/{_BARCODE}", json={"delta": 1})

    assert resp.status_code == 200
    assert resp.json()["session_total_delta"] == 3  # 1 + 2


def test_confirm_mixed_session_skips_zero_delta(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=3,
               info_status="resolved", price_status="resolved")
    _seed_item(db, _BARCODE2, session_id, delta=0,
               info_status="resolved", price_status="resolved")

    resp = client.post("/session/confirm")
    assert resp.status_code == 200
    assert resp.json()["applied_items"] == 1

    qty = db.execute("SELECT quantity FROM inventory WHERE barcode = ?", (_BARCODE,)).fetchone()
    assert qty["quantity"] == 3
    assert db.execute("SELECT quantity FROM inventory WHERE barcode = ?", (_BARCODE2,)).fetchone() is None


def test_put_delta_broadcasts_delta_update(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=1,
               info_status="resolved", price_status="resolved")

    with patch("src.api.routers.session.manager") as mock_mgr:
        mock_mgr.broadcast = AsyncMock()
        resp = client.put(f"/session/items/{_BARCODE}", json={"delta": 5})

    assert resp.status_code == 200
    mock_mgr.broadcast.assert_called_once()
    payload = json.loads(mock_mgr.broadcast.call_args[0][0])
    assert payload["type"] == "delta_update"
    assert payload["barcode"] == _BARCODE
    assert payload["retailer"] == 1
    assert payload["session_delta"] == 5
    assert payload["session_total_delta"] == 5


def test_put_delta_no_broadcast_no_session(client, db):
    with patch("src.api.routers.session.manager") as mock_mgr:
        mock_mgr.broadcast = AsyncMock()
        resp = client.put(f"/session/items/{_BARCODE}", json={"delta": 2})

    assert resp.status_code == 409
    mock_mgr.broadcast.assert_not_called()


def test_put_delta_no_broadcast_item_not_found(client, db):
    _start_session(client, "in")

    with patch("src.api.routers.session.manager") as mock_mgr:
        mock_mgr.broadcast = AsyncMock()
        resp = client.put(f"/session/items/{_BARCODE}", json={"delta": 2})

    assert resp.status_code == 404
    mock_mgr.broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# Inventory quantity in session response
# ---------------------------------------------------------------------------

def test_get_session_item_includes_inventory_quantity(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=1,
               info_status="resolved", price_status="resolved")
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    db.execute("INSERT INTO inventory (barcode, retailer_id, quantity) VALUES (?, ?, 4)", (_BARCODE, _RETAILER_ID))
    db.commit()

    resp = client.get("/session")
    assert resp.status_code == 200
    item = resp.json()["session"]["items"][0]
    assert item["inventory_quantity"] == 4


def test_get_session_item_inventory_quantity_defaults_to_zero(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=1,
               info_status="resolved", price_status="resolved")
    # No inventory row seeded

    resp = client.get("/session")
    assert resp.status_code == 200
    item = resp.json()["session"]["items"][0]
    assert item["inventory_quantity"] == 0


# ---------------------------------------------------------------------------
# off_url and price_url in GET /session items
# ---------------------------------------------------------------------------

_OFF_VIEW = "https://world.openfoodfacts.org/product/{}"
_OFF_ADD  = "https://world.openfoodfacts.org/cgi/product.pl?type=edit&code={}"


def test_get_session_item_off_url_view_when_resolved(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=1,
               info_status="resolved", price_status="resolved",
               name="Baked Beans", brand="Heinz", product_quantity="415g", price_pence=123)

    resp = client.get("/session")
    item = resp.json()["session"]["items"][0]
    assert item["off_url"] == _OFF_VIEW.format(_BARCODE)


def test_get_session_item_includes_product_page_url(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=1,
               info_status="resolved", price_status="resolved",
               name="Baked Beans", brand="Heinz", product_quantity="415g", price_pence=123)

    resp = client.get("/session")
    item = resp.json()["session"]["items"][0]
    assert item["product_page_url"] == f"/product.html?barcode={_BARCODE}&retailer_id=1"


def test_get_session_item_off_url_add_when_failed(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=1,
               info_status="failed", price_status="not_possible")

    resp = client.get("/session")
    item = resp.json()["session"]["items"][0]
    assert item["off_url"] == _OFF_ADD.format(_BARCODE)


def test_get_session_item_price_url_none_when_absent(client, db):
    session_id = _start_session(client, "in")
    _seed_item(db, _BARCODE, session_id, delta=1,
               info_status="resolved", price_status="resolved",
               name="Baked Beans", brand="Heinz", product_quantity="415g", price_pence=123)

    resp = client.get("/session")
    item = resp.json()["session"]["items"][0]
    assert item["price_url"] is None


def test_get_session_item_price_url_present_when_set(client, db):
    session_id = _start_session(client, "in")
    url = "https://www.sainsburys.co.uk/gol-ui/product/baked-beans"
    _seed_item(db, _BARCODE, session_id, delta=1,
               info_status="resolved", price_status="resolved",
               name="Baked Beans", brand="Heinz", product_quantity="415g",
               price_pence=123, product_url=url)

    resp = client.get("/session")
    item = resp.json()["session"]["items"][0]
    assert item["price_url"] == url


# ---------------------------------------------------------------------------
# Item 7: recovered_at appears in GET /session after simulated restart
# ---------------------------------------------------------------------------

def test_get_session_recovered_at_appears_in_response(client, db):
    session_id = _start_session(client)
    db.execute(
        "UPDATE sessions SET recovered_at = '2026-06-01T09:00:00' WHERE id = ?",
        (session_id,)
    )
    db.commit()
    resp = client.get("/session")
    assert resp.status_code == 200
    data = resp.json()["session"]
    assert data["recovered_at"] == "2026-06-01T09:00:00"
    assert data["retailer"] == 1

