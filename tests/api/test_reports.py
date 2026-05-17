import sqlite3

import pytest
from starlette.testclient import TestClient

from src.api.dependencies import get_db, verify_api_key
from src.api.main import app

SCHEMA = """
CREATE TABLE canonical_products (id INTEGER PRIMARY KEY);
CREATE TABLE retailers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE product_variants (
    id INTEGER PRIMARY KEY,
    canonical_product_id INTEGER REFERENCES canonical_products(id),
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    name TEXT,
    brand TEXT,
    weight_g REAL
);
CREATE TABLE prices (
    id INTEGER PRIMARY KEY,
    product_variant_id INTEGER NOT NULL REFERENCES product_variants(id),
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    price_pence INTEGER,
    price_type TEXT NOT NULL DEFAULT 'unit',
    last_updated TEXT
);
CREATE TABLE inventory (
    id INTEGER PRIMARY KEY,
    product_variant_id INTEGER NOT NULL UNIQUE REFERENCES product_variants(id),
    quantity INTEGER NOT NULL DEFAULT 0,
    minimum_quantity INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO retailers (id, name) VALUES (1, 'Sainsbury''s');
INSERT INTO config (key, value) VALUES ('scan_mode', 'out');
"""


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


# ---------------------------------------------------------------------------
# GET /reports/inventory
# ---------------------------------------------------------------------------

def test_inventory_empty(client):
    resp = client.get("/reports/inventory")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total_value_pence": 0}


def test_inventory_single_item_with_price(client, db):
    db.execute("INSERT INTO product_variants (id, retailer_id, name, brand) VALUES (1, 1, 'Oat Milk', 'Oatly')")
    db.execute("INSERT INTO prices (id, product_variant_id, retailer_id, price_pence) VALUES (1, 1, 1, 110)")
    db.execute("INSERT INTO inventory (product_variant_id, quantity, minimum_quantity) VALUES (1, 3, 1)")
    db.commit()

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
    db.execute("INSERT INTO product_variants (id, retailer_id, name) VALUES (1, 1, 'Mystery Item')")
    db.execute("INSERT INTO prices (id, product_variant_id, retailer_id, price_pence) VALUES (1, 1, 1, NULL)")
    db.execute("INSERT INTO inventory (product_variant_id, quantity) VALUES (1, 2)")
    db.commit()

    resp = client.get("/reports/inventory")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["price_pence"] is None
    assert item["line_total_pence"] is None
    assert data["total_value_pence"] == 0


def test_inventory_mixed_priced_and_unpriced(client, db):
    db.execute("INSERT INTO product_variants (id, retailer_id, name) VALUES (1, 1, 'Butter')")
    db.execute("INSERT INTO product_variants (id, retailer_id, name) VALUES (2, 1, 'Salt')")
    db.execute("INSERT INTO prices (id, product_variant_id, retailer_id, price_pence) VALUES (1, 1, 1, 110)")
    db.execute("INSERT INTO prices (id, product_variant_id, retailer_id, price_pence) VALUES (2, 2, 1, NULL)")
    db.execute("INSERT INTO inventory (product_variant_id, quantity) VALUES (1, 2)")
    db.execute("INSERT INTO inventory (product_variant_id, quantity) VALUES (2, 1)")
    db.commit()

    resp = client.get("/reports/inventory")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["total_value_pence"] == 220


def test_inventory_sorted_by_name(client, db):
    db.execute("INSERT INTO product_variants (id, retailer_id, name) VALUES (1, 1, 'Zucchini')")
    db.execute("INSERT INTO product_variants (id, retailer_id, name) VALUES (2, 1, 'Apple')")
    db.execute("INSERT INTO inventory (product_variant_id, quantity) VALUES (1, 1)")
    db.execute("INSERT INTO inventory (product_variant_id, quantity) VALUES (2, 1)")
    db.commit()

    resp = client.get("/reports/inventory")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["name"] == "Apple"
    assert items[1]["name"] == "Zucchini"


# ---------------------------------------------------------------------------
# GET /reports/low-stock
# ---------------------------------------------------------------------------

def test_low_stock_none(client, db):
    db.execute("INSERT INTO product_variants (id, retailer_id, name) VALUES (1, 1, 'Rice')")
    db.execute("INSERT INTO inventory (product_variant_id, quantity, minimum_quantity) VALUES (1, 5, 2)")
    db.commit()

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_low_stock_one_item(client, db):
    db.execute("INSERT INTO product_variants (id, retailer_id, name, brand) VALUES (1, 1, 'Red Lentils', 'Laila')")
    db.execute("INSERT INTO inventory (product_variant_id, quantity, minimum_quantity) VALUES (1, 1, 3)")
    db.commit()

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["quantity"] == 1
    assert item["minimum_quantity"] == 3
    assert item["shortfall"] == 2


def test_low_stock_excludes_exactly_at_minimum(client, db):
    db.execute("INSERT INTO product_variants (id, retailer_id, name) VALUES (1, 1, 'Pasta')")
    db.execute("INSERT INTO inventory (product_variant_id, quantity, minimum_quantity) VALUES (1, 2, 2)")
    db.commit()

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_low_stock_excludes_zero_minimum(client, db):
    db.execute("INSERT INTO product_variants (id, retailer_id, name) VALUES (1, 1, 'Herbs')")
    db.execute("INSERT INTO inventory (product_variant_id, quantity, minimum_quantity) VALUES (1, 0, 0)")
    db.commit()

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_low_stock_sorted_by_shortfall_desc(client, db):
    db.execute("INSERT INTO product_variants (id, retailer_id, name) VALUES (1, 1, 'Item A')")
    db.execute("INSERT INTO product_variants (id, retailer_id, name) VALUES (2, 1, 'Item B')")
    db.execute("INSERT INTO inventory (product_variant_id, quantity, minimum_quantity) VALUES (1, 2, 3)")
    db.execute("INSERT INTO inventory (product_variant_id, quantity, minimum_quantity) VALUES (2, 0, 3)")
    db.commit()

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert items[0]["name"] == "Item B"
    assert items[0]["shortfall"] == 3
    assert items[1]["name"] == "Item A"
    assert items[1]["shortfall"] == 1
