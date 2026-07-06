"""Tests for src/api/printer.py — ESC/POS formatting and print endpoint."""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from src.api.dependencies import get_db, verify_api_key
from src.api.main import app
from src.api.printer import PrinterUnavailableError, _format_inventory, _format_low_stock, _get_printer

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, name TEXT, brand TEXT, minimum_quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit', product_url TEXT NULL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE sessions (id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('in', 'out')), started_at TEXT NOT NULL, recovered_at TEXT);
CREATE TABLE session_items (session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL, delta INTEGER NOT NULL CHECK(delta >= 0), info_status TEXT NOT NULL DEFAULT 'pending' CHECK(info_status IN ('pending', 'resolved', 'failed')), price_status TEXT NOT NULL DEFAULT 'pending' CHECK(price_status IN ('pending', 'resolved', 'failed', 'not_possible')), first_scanned_at TEXT NOT NULL, PRIMARY KEY (session_id, barcode, retailer_id));
CREATE TABLE worker_state (id INTEGER PRIMARY KEY CHECK(id = 1), off_last_called_at TEXT NOT NULL);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE product_groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, minimum_quantity INTEGER NOT NULL DEFAULT 0 CHECK(minimum_quantity >= 0));
CREATE TABLE group_variant_members (group_id INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE, barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, PRIMARY KEY (group_id, barcode, retailer_id), FOREIGN KEY (barcode, retailer_id) REFERENCES product_variants(barcode, retailer_id) ON DELETE CASCADE);
CREATE TABLE group_group_members (parent_group_id INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE, child_group_id INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE, PRIMARY KEY (parent_group_id, child_group_id), CHECK (parent_group_id != child_group_id));
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO worker_state (id, off_last_called_at) VALUES (1, '1970-01-01T00:00:00');
"""

_RETAILER_ID = 1


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
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client_no_auth(db):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    app.state.sainsburys_retailer_id = _RETAILER_ID
    with patch("src.api.dependencies._API_KEY", "test-secret"):
        yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Formatter fixtures
# ---------------------------------------------------------------------------

INVENTORY_TWO_ITEMS = {
    "items": [
        {"name": "Baked Beans", "brand": "Heinz", "quantity": 3, "price_pence": 123, "line_total_pence": 369},
        {"name": "Kidney Beans", "brand": None, "quantity": 2, "price_pence": None, "line_total_pence": None},
    ],
    "total_value_pence": 369,
}

INVENTORY_ALL_PRICED = {
    "items": [
        {"name": "Apple", "brand": None, "quantity": 1, "price_pence": 123, "line_total_pence": 123},
        {"name": "Banana", "brand": None, "quantity": 1, "price_pence": 123, "line_total_pence": 123},
    ],
    "total_value_pence": 246,
}

INVENTORY_NO_PRICES = {
    "items": [
        {"name": "Apple", "brand": None, "quantity": 1, "price_pence": None, "line_total_pence": None},
    ],
    "total_value_pence": 0,
}

LOW_STOCK_REPORT = {
    "groups": [
        {"group_id": 1, "name": "Bean Collection", "have": 2, "need": 5, "short": 3},
    ],
    "products": [
        {"barcode": "1", "name": "Baked Beans", "brand": "Heinz", "have": 3, "need": 5, "short": 2},
        {"barcode": "2", "name": "Kidney Beans", "brand": None, "have": 0, "need": 3, "short": 3},
    ],
}

LOW_STOCK_EMPTY = {"groups": [], "products": []}


# ---------------------------------------------------------------------------
# Formatting tests (tests 1–5)
# ---------------------------------------------------------------------------

def test_format_inventory_names_and_prices():
    raw = _format_inventory(INVENTORY_TWO_ITEMS)
    assert b"Baked Beans" in raw
    assert b"Heinz" in raw
    assert b"1.23" in raw
    assert b"?" in raw  # em dash substituted as ? by CP437 Dummy for no-price item


def test_format_inventory_total_value():
    raw = _format_inventory(INVENTORY_ALL_PRICED)
    assert b"2.46" in raw


def test_format_inventory_no_prices():
    raw = _format_inventory(INVENTORY_NO_PRICES)
    assert b"No prices available" in raw


def test_format_low_stock_names_and_quantities():
    raw = _format_low_stock(LOW_STOCK_REPORT)
    assert b"Bean Collection" in raw
    assert b"Baked Beans" in raw
    assert b"Kidney Beans" in raw
    assert b"3" in raw
    assert b"5" in raw


def test_format_low_stock_sections_and_no_footer():
    """Groups section precedes Products section; the old 'n items below minimum' footer is gone."""
    raw = _format_low_stock(LOW_STOCK_REPORT)
    assert b"GROUPS" in raw
    assert b"PRODUCTS" in raw
    assert raw.index(b"GROUPS") < raw.index(b"PRODUCTS")
    # The removed footer read "<n> item(s) below minimum" — assert that phrasing is gone.
    assert b"items below minimum" not in raw
    assert b"item below minimum" not in raw


def test_format_low_stock_empty():
    raw = _format_low_stock(LOW_STOCK_EMPTY)
    assert b"No groups below minimum" in raw
    assert b"No products below minimum" in raw


# ---------------------------------------------------------------------------
# Endpoint tests (tests 6–9, 13)
# ---------------------------------------------------------------------------

def test_post_print_inventory_200(client):
    with patch("src.api.printer.print_inventory", MagicMock(return_value=None)):
        res = client.post("/print/inventory")
    assert res.status_code == 200
    assert res.json() == {"printed": True}


def test_post_print_inventory_503_unavailable(client):
    err = PrinterUnavailableError("tcp fail", code="printer_unavailable")
    with patch("src.api.printer.print_inventory", side_effect=err):
        res = client.post("/print/inventory")
    assert res.status_code == 503
    assert res.json() == {"error": "printer_unavailable"}


def test_post_print_low_stock_200(client):
    with patch("src.api.printer.print_low_stock", MagicMock(return_value=None)):
        res = client.post("/print/low-stock")
    assert res.status_code == 200
    assert res.json() == {"printed": True}


def test_post_print_low_stock_503_unavailable(client):
    err = PrinterUnavailableError("tcp fail", code="printer_unavailable")
    with patch("src.api.printer.print_low_stock", side_effect=err):
        res = client.post("/print/low-stock")
    assert res.status_code == 503
    assert res.json() == {"error": "printer_unavailable"}


def test_post_print_inventory_503_not_configured(client):
    err = PrinterUnavailableError("not set", code="printer_not_configured")
    with patch("src.api.printer.print_inventory", side_effect=err):
        res = client.post("/print/inventory")
    assert res.status_code == 503
    assert res.json() == {"error": "printer_not_configured"}


# ---------------------------------------------------------------------------
# _get_printer raises correct error when PRINTER_IP unset (test 10)
# ---------------------------------------------------------------------------

def test_get_printer_raises_when_ip_unset():
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(PrinterUnavailableError) as exc_info:
            _get_printer()
    assert exc_info.value.code == "printer_not_configured"


# ---------------------------------------------------------------------------
# Auth tests (tests 11–12)
# ---------------------------------------------------------------------------

def test_post_print_inventory_401_no_auth(client_no_auth):
    res = client_no_auth.post("/print/inventory")
    assert res.status_code == 401


def test_post_print_low_stock_401_no_auth(client_no_auth):
    res = client_no_auth.post("/print/low-stock")
    assert res.status_code == 401
