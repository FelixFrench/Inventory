"""Tests for src/api/routers/products.py — product minimum-quantity management."""

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
CREATE TABLE product_variants (barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL REFERENCES retailers(id), name TEXT, brand TEXT, product_quantity TEXT, minimum_quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
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
    """Client without verify_api_key bypass — used to test 401 responses."""
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
# GET /products/minimum-quantities
# ---------------------------------------------------------------------------

def test_get_minimum_quantities_ordered(client, db):
    """Products are returned ordered by name ASC NULLS LAST, then barcode ASC."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('1111111111111');
        INSERT INTO barcodes VALUES ('2222222222222');
        INSERT INTO barcodes VALUES ('3333333333333');
        INSERT INTO inventory (barcode, retailer_id, quantity) VALUES ('1111111111111', 1, 3);
        INSERT INTO inventory (barcode, retailer_id, quantity) VALUES ('2222222222222', 1, 1);
        INSERT INTO inventory (barcode, retailer_id, quantity) VALUES ('3333333333333', 1, 2);
        INSERT INTO product_variants (barcode, retailer_id, name, brand, product_quantity, minimum_quantity) VALUES ('1111111111111', 1, 'Baked Beans', 'Heinz', '415g', 2);
        INSERT INTO product_variants (barcode, retailer_id, name, brand, product_quantity, minimum_quantity) VALUES ('2222222222222', 1, 'Apple Juice', 'Tropicana', '1kg', 1);
    """)
    resp = client.get("/products/minimum-quantities")
    assert resp.status_code == 200
    products = resp.json()["products"]
    assert len(products) == 3
    assert products[0]["name"] == "Apple Juice"
    assert products[1]["name"] == "Baked Beans"
    # The unresolved barcode (no product_variants row) sorts last
    assert products[2]["name"] is None
    assert products[2]["barcode"] == "3333333333333"


def test_get_minimum_quantities_no_variant_row(client, db):
    """Products without a product_variants row have name/brand/quantity as null."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('9999999999999');
        INSERT INTO inventory (barcode, retailer_id, quantity) VALUES ('9999999999999', 1, 1);
    """)
    resp = client.get("/products/minimum-quantities")
    assert resp.status_code == 200
    p = resp.json()["products"][0]
    assert p["name"] is None
    assert p["brand"] is None
    assert p["quantity"] is None
    assert p["barcode"] == "9999999999999"


def test_get_minimum_quantities_coalesce_null(client, db):
    """minimum_quantity defaults to 0 when the DB value is null (schema DEFAULT covers this,
    but the COALESCE ensures it regardless)."""
    db.execute("INSERT INTO barcodes VALUES ('1234567890123')")
    db.execute("INSERT INTO inventory (barcode, retailer_id, quantity) VALUES ('1234567890123', 1, 5)")
    db.commit()
    resp = client.get("/products/minimum-quantities")
    assert resp.status_code == 200
    p = resp.json()["products"][0]
    assert p["minimum_quantity"] == 0
    assert p["current_quantity"] == 5


def test_get_minimum_quantities_quantity_grams(client, db):
    """product_quantity string is returned verbatim."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('1000000000001');
        INSERT INTO inventory (barcode, retailer_id) VALUES ('1000000000001', 1);
        INSERT INTO product_variants (barcode, retailer_id, name, brand, product_quantity) VALUES ('1000000000001', 1, 'Soup', 'Heinz', '415g');
    """)
    resp = client.get("/products/minimum-quantities")
    p = resp.json()["products"][0]
    assert p["quantity"] == "415g"


def test_get_minimum_quantities_quantity_kg(client, db):
    """product_quantity string is returned verbatim for kg products."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('1000000000002');
        INSERT INTO inventory (barcode, retailer_id) VALUES ('1000000000002', 1);
        INSERT INTO product_variants (barcode, retailer_id, name, brand, product_quantity) VALUES ('1000000000002', 1, 'Milk', 'Arla', '1.5kg');
    """)
    resp = client.get("/products/minimum-quantities")
    p = resp.json()["products"][0]
    assert p["quantity"] == "1.5kg"


def test_get_minimum_quantities_empty_inventory(client, db):
    """Empty inventory returns an empty products list."""
    resp = client.get("/products/minimum-quantities")
    assert resp.status_code == 200
    assert resp.json() == {"products": []}


def test_get_minimum_quantities_requires_auth(client_no_auth):
    """Returns 401 when no API key header is provided."""
    resp = client_no_auth.get("/products/minimum-quantities")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# PUT /products/{barcode}/minimum_quantity
# ---------------------------------------------------------------------------

def test_put_minimum_quantity_updates_existing_variant(client, db):
    """PUT updates the minimum on a barcode whose product_variants row already exists."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_variants (barcode, retailer_id, name, minimum_quantity) VALUES ('5014788110140', 1, 'Beans', 1);
        INSERT INTO inventory (barcode, retailer_id, quantity) VALUES ('5014788110140', 1, 4);
    """)
    resp = client.put(
        "/products/5014788110140/minimum_quantity",
        json={"minimum_quantity": 3},
    )
    assert resp.status_code == 200
    assert resp.json() == {"barcode": "5014788110140", "minimum_quantity": 3}
    row = db.execute(
        "SELECT name, minimum_quantity FROM product_variants WHERE barcode = '5014788110140' AND retailer_id = 1"
    ).fetchone()
    assert row["minimum_quantity"] == 3
    assert row["name"] == "Beans"  # existing OFF data is not clobbered by the upsert


def test_put_minimum_quantity_creates_variant_for_in_system_barcode(client, db):
    """PUT creates a null-data variant row for an in-system barcode (barcodes row present)
    that has no variant/inventory row; the minimum is readable afterward."""
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    resp = client.put(
        "/products/5014788110140/minimum_quantity",
        json={"minimum_quantity": 4},
    )
    assert resp.status_code == 200
    assert resp.json() == {"barcode": "5014788110140", "minimum_quantity": 4}
    row = db.execute(
        "SELECT name, brand, product_quantity, minimum_quantity FROM product_variants "
        "WHERE barcode = '5014788110140' AND retailer_id = 1"
    ).fetchone()
    assert row is not None
    assert row["minimum_quantity"] == 4
    # OFF-sourced fields left null (same null-data shape as the migration carry row)
    assert row["name"] is None and row["brand"] is None and row["product_quantity"] is None


def test_put_minimum_quantity_zero_is_valid(client, db):
    """minimum_quantity=0 is a valid value (means no minimum)."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_variants (barcode, retailer_id, name, minimum_quantity) VALUES ('5014788110140', 1, 'Beans', 5);
    """)
    resp = client.put(
        "/products/5014788110140/minimum_quantity",
        json={"minimum_quantity": 0},
    )
    assert resp.status_code == 200
    assert resp.json()["minimum_quantity"] == 0
    row = db.execute(
        "SELECT minimum_quantity FROM product_variants WHERE barcode = '5014788110140' AND retailer_id = 1"
    ).fetchone()
    assert row["minimum_quantity"] == 0


def test_put_minimum_quantity_negative_returns_422(client, db):
    """minimum_quantity < 0 is rejected at the schema layer (Field(ge=0) → 422)."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_variants (barcode, retailer_id) VALUES ('5014788110140', 1);
    """)
    resp = client.put(
        "/products/5014788110140/minimum_quantity",
        json={"minimum_quantity": -1},
    )
    assert resp.status_code == 422


def test_put_minimum_quantity_float_returns_422(client, db):
    """minimum_quantity=1.5 returns 422 — Pydantic rejects non-integer."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_variants (barcode, retailer_id) VALUES ('5014788110140', 1);
    """)
    resp = client.put(
        "/products/5014788110140/minimum_quantity",
        json={"minimum_quantity": 1.5},
    )
    assert resp.status_code == 422  # Pydantic validation error


def test_put_minimum_quantity_unknown_barcode_returns_404(client, db):
    """A barcode with no barcodes row violates the product_variants FK → 404 (not 500)."""
    resp = client.put(
        "/products/0000000000000/minimum_quantity",
        json={"minimum_quantity": 2},
    )
    assert resp.status_code == 404
    assert resp.json() == {"error": "barcode_not_found"}


def test_put_minimum_quantity_requires_auth(client_no_auth, db):
    """Returns 401 when no API key header is provided."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_variants (barcode, retailer_id) VALUES ('5014788110140', 1);
    """)
    resp = client_no_auth.put(
        "/products/5014788110140/minimum_quantity",
        json={"minimum_quantity": 1},
    )
    assert resp.status_code == 401
