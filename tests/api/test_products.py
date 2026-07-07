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
# PUT /products/{barcode}/{retailer_id}/minimum_quantity
# ---------------------------------------------------------------------------

def test_put_minimum_quantity_updates_existing_variant(client, db):
    """PUT updates the minimum on a barcode whose product_variants row already exists."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_variants (barcode, retailer_id, name, minimum_quantity) VALUES ('5014788110140', 1, 'Beans', 1);
        INSERT INTO inventory (barcode, retailer_id, quantity) VALUES ('5014788110140', 1, 4);
    """)
    resp = client.put(
        "/products/5014788110140/1/minimum_quantity",
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
        "/products/5014788110140/1/minimum_quantity",
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
        "/products/5014788110140/1/minimum_quantity",
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
        "/products/5014788110140/1/minimum_quantity",
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
        "/products/5014788110140/1/minimum_quantity",
        json={"minimum_quantity": 1.5},
    )
    assert resp.status_code == 422  # Pydantic validation error


def test_put_minimum_quantity_unknown_barcode_returns_404(client, db):
    """A barcode with no barcodes row violates the product_variants FK → 404 (not 500)."""
    resp = client.put(
        "/products/0000000000000/1/minimum_quantity",
        json={"minimum_quantity": 2},
    )
    assert resp.status_code == 404
    assert resp.json() == {"error": "barcode_not_found"}


def test_put_minimum_quantity_unknown_retailer_404(client, db):
    """An unknown retailer_id is surfaced as retailer_not_found, checked before the upsert so
    no variant row is created for the bad retailer."""
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    resp = client.put(
        "/products/5014788110140/999/minimum_quantity",
        json={"minimum_quantity": 2},
    )
    assert resp.status_code == 404
    assert resp.json() == {"error": "retailer_not_found"}
    # The guard runs before the upsert: nothing was written for the bad retailer.
    assert db.execute("SELECT 1 FROM product_variants WHERE retailer_id=999").fetchone() is None


def test_put_minimum_quantity_requires_auth(client_no_auth, db):
    """Returns 401 when no API key header is provided."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_variants (barcode, retailer_id) VALUES ('5014788110140', 1);
    """)
    resp = client_no_auth.put(
        "/products/5014788110140/1/minimum_quantity",
        json={"minimum_quantity": 1},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /products/{barcode}/{retailer_id} — product detail
# ---------------------------------------------------------------------------

def test_product_detail_full(client, db):
    """Returns variant fields, current inventory, minimum, price fields, and memberships."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_variants (barcode, retailer_id, name, brand, product_quantity, minimum_quantity)
            VALUES ('5014788110140', 1, 'Baked Beans', 'Heinz', '415g', 3);
        INSERT INTO inventory (barcode, retailer_id, quantity) VALUES ('5014788110140', 1, 2);
        INSERT INTO prices (barcode, retailer_id, price_pence, price_type, product_url)
            VALUES ('5014788110140', 1, 120, 'unit', 'https://example.test/x');
        INSERT INTO product_groups (id, name, minimum_quantity) VALUES (7, 'Beans', 5);
        INSERT INTO group_variant_members (group_id, barcode, retailer_id) VALUES (7, '5014788110140', 1);
    """)
    resp = client.get("/products/5014788110140/1")
    assert resp.status_code == 200
    d = resp.json()
    assert d["barcode"] == "5014788110140"
    assert d["name"] == "Baked Beans"
    assert d["brand"] == "Heinz"
    assert d["product_quantity"] == "415g"
    assert d["minimum_quantity"] == 3
    assert d["current_quantity"] == 2
    assert d["price_pence"] == 120
    assert d["price_type"] == "unit"
    assert d["product_url"] == "https://example.test/x"
    # OFF link is built server-side via urls.py's off_url() (view URL since a name is present).
    assert d["off_url"] == "https://world.openfoodfacts.org/product/5014788110140"
    assert d["groups"] == [
        {"id": 7, "name": "Beans", "group_page_url": "/group.html?id=7"}
    ]


def test_product_detail_nulls_render(client, db):
    """A barcode with no product_variants/inventory/prices row still renders (all nullable)."""
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    resp = client.get("/products/5014788110140/1")
    assert resp.status_code == 200
    d = resp.json()
    assert d["name"] is None
    assert d["brand"] is None
    assert d["product_quantity"] is None
    assert d["minimum_quantity"] == 0
    assert d["current_quantity"] == 0
    assert d["price_pence"] is None
    assert d["price_type"] is None
    # No name yet -> off_url falls back to the add/edit URL.
    assert d["off_url"] == (
        "https://world.openfoodfacts.org/cgi/product.pl?type=edit&code=5014788110140"
    )
    assert d["groups"] == []


def test_product_detail_not_found(client):
    resp = client.get("/products/0000000000000/1")
    assert resp.status_code == 404
    assert resp.json() == {"error": "barcode_not_found"}


def test_product_detail_unknown_retailer_404(client, db):
    """A valid barcode with a nonexistent retailer_id returns 404 retailer_not_found (not a
    misleading all-null 200)."""
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    resp = client.get("/products/5014788110140/999")
    assert resp.status_code == 404
    assert resp.json() == {"error": "retailer_not_found"}


def test_product_detail_non_numeric_retailer_422(client, db):
    """A non-numeric retailer_id segment fails int path validation (422)."""
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    resp = client.get("/products/5014788110140/abc")
    assert resp.status_code == 422


def test_product_detail_requires_auth(client_no_auth, db):
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    resp = client_no_auth.get("/products/5014788110140/1")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Product-side membership: POST/DELETE /products/{barcode}/{retailer_id}/groups
# ---------------------------------------------------------------------------

def test_product_add_group_membership(client, db):
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_groups (id, name) VALUES (1, 'G');
    """)
    resp = client.post("/products/5014788110140/1/groups", json={"group_id": 1})
    assert resp.status_code == 200
    # Edge exists, and a null-data variant row was upserted.
    edge = db.execute(
        "SELECT 1 FROM group_variant_members WHERE group_id=1 AND barcode='5014788110140' AND retailer_id=1"
    ).fetchone()
    assert edge is not None
    pv = db.execute(
        "SELECT name FROM product_variants WHERE barcode='5014788110140' AND retailer_id=1"
    ).fetchone()
    assert pv is not None and pv["name"] is None


def test_product_add_group_unknown_barcode_404(client, db):
    db.execute("INSERT INTO product_groups (id, name) VALUES (1, 'G')")
    db.commit()
    resp = client.post("/products/0000000000000/1/groups", json={"group_id": 1})
    assert resp.status_code == 404
    assert resp.json() == {"error": "barcode_not_found"}


def test_product_add_group_unknown_group_404(client, db):
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    resp = client.post("/products/5014788110140/1/groups", json={"group_id": 999})
    assert resp.status_code == 404
    assert resp.json() == {"error": "group_not_found"}


def test_product_add_group_unknown_retailer_404(client, db):
    """An unknown retailer_id is surfaced as retailer_not_found, NOT misattributed to
    barcode_not_found (the product_variants -> retailers FK would otherwise raise IntegrityError)."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_groups (id, name) VALUES (1, 'G');
    """)
    resp = client.post("/products/5014788110140/999/groups", json={"group_id": 1})
    assert resp.status_code == 404
    assert resp.json() == {"error": "retailer_not_found"}
    # No edge or variant row was written for the bad retailer.
    assert db.execute("SELECT 1 FROM group_variant_members").fetchone() is None
    assert db.execute("SELECT 1 FROM product_variants WHERE retailer_id=999").fetchone() is None


def test_product_remove_group_membership(client, db):
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_groups (id, name) VALUES (1, 'G');
        INSERT INTO product_variants (barcode, retailer_id) VALUES ('5014788110140', 1);
        INSERT INTO group_variant_members (group_id, barcode, retailer_id) VALUES (1, '5014788110140', 1);
    """)
    resp = client.delete("/products/5014788110140/1/groups/1")
    assert resp.status_code == 200
    edge = db.execute(
        "SELECT 1 FROM group_variant_members WHERE group_id=1 AND barcode='5014788110140'"
    ).fetchone()
    assert edge is None
    # The variant and the group both survive.
    assert db.execute("SELECT 1 FROM product_variants WHERE barcode='5014788110140'").fetchone() is not None
    assert db.execute("SELECT 1 FROM product_groups WHERE id=1").fetchone() is not None


def test_product_remove_group_unknown_group_404(client, db):
    """A non-existent group id is surfaced (404), not a silent zero-row no-op."""
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    resp = client.delete("/products/5014788110140/1/groups/999")
    assert resp.status_code == 404
    assert resp.json() == {"error": "group_not_found"}


def test_product_remove_group_unknown_retailer_404(client, db):
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_groups (id, name) VALUES (1, 'G');
    """)
    resp = client.delete("/products/5014788110140/999/groups/1")
    assert resp.status_code == 404
    assert resp.json() == {"error": "retailer_not_found"}


def test_product_remove_group_valid_nonmember_is_noop(client, db):
    """Removing a valid barcode+group that was never a member is an idempotent 200 no-op."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_groups (id, name) VALUES (1, 'G');
    """)
    resp = client.delete("/products/5014788110140/1/groups/1")
    assert resp.status_code == 200


def test_product_add_group_membership_requires_auth(client_no_auth, db):
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_groups (id, name) VALUES (1, 'G');
    """)
    resp = client_no_auth.post("/products/5014788110140/1/groups", json={"group_id": 1})
    assert resp.status_code == 401
