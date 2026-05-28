import sqlite3

import pytest
from starlette.testclient import TestClient

from src.api.dependencies import get_db, verify_api_key
from src.api.main import app

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, name TEXT, brand TEXT, weight_g REAL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit', PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT PRIMARY KEY, quantity INTEGER NOT NULL DEFAULT 0, minimum_quantity INTEGER NOT NULL DEFAULT 0);
CREATE TABLE sessions (id INTEGER PRIMARY KEY, type TEXT NOT NULL, started_at TEXT NOT NULL, recovered_at TEXT);
CREATE TABLE session_items (session_id INTEGER NOT NULL, barcode TEXT NOT NULL, delta INTEGER NOT NULL, info_status TEXT NOT NULL DEFAULT 'pending', price_status TEXT NOT NULL DEFAULT 'pending', first_scanned_at TEXT NOT NULL, PRIMARY KEY (session_id, barcode));
CREATE TABLE worker_state (id INTEGER PRIMARY KEY, off_last_called_at TEXT NOT NULL);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_item(db, barcode: str, name: str, brand: str = None,
               quantity: int = 0, minimum_quantity: int = 0, price_pence: int = None):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (barcode,))
    db.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name, brand) VALUES (?, ?, ?, ?)",
        (barcode, _RETAILER_ID, name, brand)
    )
    db.execute(
        "INSERT INTO inventory (barcode, quantity, minimum_quantity) VALUES (?, ?, ?)",
        (barcode, quantity, minimum_quantity)
    )
    if price_pence is not None:
        db.execute(
            "INSERT INTO prices (barcode, retailer_id, price_pence, price_type) VALUES (?, ?, ?, 'unit')",
            (barcode, _RETAILER_ID, price_pence)
        )
    db.commit()


# ---------------------------------------------------------------------------
# GET /reports/inventory
# ---------------------------------------------------------------------------

def test_inventory_empty(client):
    resp = client.get("/reports/inventory")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total_value_pence": 0}


def test_inventory_single_item_with_price(client, db):
    _seed_item(db, "5000000000001", "Oat Milk", brand="Oatly", quantity=3, minimum_quantity=1, price_pence=110)

    resp = client.get("/reports/inventory")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["name"] == "Oat Milk"
    assert item["brand"] == "Oatly"
    assert item["quantity"] == 3
    assert item["price_pence"] == 110
    assert item["line_total_pence"] == 330
    assert data["total_value_pence"] == 330


def test_inventory_single_item_no_price(client, db):
    _seed_item(db, "5000000000002", "Mystery Item", quantity=2)

    resp = client.get("/reports/inventory")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["price_pence"] is None
    assert item["line_total_pence"] is None
    assert data["total_value_pence"] == 0


def test_inventory_mixed_priced_and_unpriced(client, db):
    _seed_item(db, "5000000000003", "Butter", quantity=2, price_pence=110)
    _seed_item(db, "5000000000004", "Salt", quantity=1)

    resp = client.get("/reports/inventory")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["total_value_pence"] == 220


def test_inventory_sorted_by_name(client, db):
    _seed_item(db, "5000000000005", "Zucchini", quantity=1)
    _seed_item(db, "5000000000006", "Apple", quantity=1)

    resp = client.get("/reports/inventory")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["name"] == "Apple"
    assert items[1]["name"] == "Zucchini"


# ---------------------------------------------------------------------------
# GET /reports/low-stock
# ---------------------------------------------------------------------------

def test_low_stock_none(client, db):
    _seed_item(db, "5000000000007", "Rice", quantity=5, minimum_quantity=2)

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_low_stock_one_item(client, db):
    _seed_item(db, "5000000000008", "Red Lentils", brand="Laila", quantity=1, minimum_quantity=3)

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["quantity"] == 1
    assert item["minimum_quantity"] == 3
    assert item["shortfall"] == 2


def test_low_stock_excludes_exactly_at_minimum(client, db):
    _seed_item(db, "5000000000009", "Pasta", quantity=2, minimum_quantity=2)

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_low_stock_excludes_zero_minimum(client, db):
    _seed_item(db, "5000000000010", "Herbs", quantity=0, minimum_quantity=0)

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_low_stock_sorted_by_shortfall_desc(client, db):
    _seed_item(db, "5000000000011", "Item A", quantity=2, minimum_quantity=3)
    _seed_item(db, "5000000000012", "Item B", quantity=0, minimum_quantity=3)

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert items[0]["name"] == "Item B"
    assert items[0]["shortfall"] == 3
    assert items[1]["name"] == "Item A"
    assert items[1]["shortfall"] == 1
