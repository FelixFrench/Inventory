"""Tests for src/api/reports.py — inventory, low-stock, and unresolved reports."""

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
CREATE TABLE product_variants (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, name TEXT, brand TEXT, product_quantity TEXT, minimum_quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit', product_url TEXT NULL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE sessions (id INTEGER PRIMARY KEY, type TEXT NOT NULL, started_at TEXT NOT NULL, recovered_at TEXT);
CREATE TABLE session_items (session_id INTEGER NOT NULL, barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, delta INTEGER NOT NULL, info_status TEXT NOT NULL DEFAULT 'pending', price_status TEXT NOT NULL DEFAULT 'pending', first_scanned_at TEXT NOT NULL, PRIMARY KEY (session_id, barcode, retailer_id));
CREATE TABLE worker_state (id INTEGER PRIMARY KEY, off_last_called_at TEXT NOT NULL);
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


def _seed_item(db, barcode: str, name: str, brand: str | None = None,
               quantity: int = 0, minimum_quantity: int = 0, price_pence: int | None = None,
               product_url: str | None = None):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (barcode,))
    db.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name, brand, minimum_quantity) VALUES (?, ?, ?, ?, ?)",
        (barcode, _RETAILER_ID, name, brand, minimum_quantity)
    )
    db.execute(
        "INSERT INTO inventory (barcode, retailer_id, quantity) VALUES (?, ?, ?)",
        (barcode, _RETAILER_ID, quantity)
    )
    if price_pence is not None:
        db.execute(
            "INSERT INTO prices (barcode, retailer_id, price_pence, price_type, product_url) VALUES (?, ?, ?, 'unit', ?)",
            (barcode, _RETAILER_ID, price_pence, product_url)
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


def _seed_off_failed_item(db, barcode: str, quantity: int):
    """Insert a barcode+inventory row only — no product_variants, no prices."""
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (barcode,))
    db.execute(
        "INSERT INTO inventory (barcode, retailer_id, quantity) VALUES (?, ?, ?)",
        (barcode, _RETAILER_ID, quantity)
    )
    db.commit()


def test_inventory_off_failed_item_appears(client, db):
    barcode = "5000000000099"
    _seed_off_failed_item(db, barcode, quantity=2)

    resp = client.get("/reports/inventory")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["name"] == barcode
    assert item["brand"] is None
    assert item["price_pence"] is None
    assert item["line_total_pence"] is None


def test_inventory_off_failed_item_contributes_zero_to_total(client, db):
    _seed_off_failed_item(db, "5000000000098", quantity=5)

    resp = client.get("/reports/inventory")
    assert resp.status_code == 200
    assert resp.json()["total_value_pence"] == 0


def test_inventory_mixed_resolved_and_failed(client, db):
    failed_barcode = "5000000000097"
    _seed_item(db, "5000000000096", "Cheddar", brand="Cathedral City", quantity=1, price_pence=300)
    _seed_off_failed_item(db, failed_barcode, quantity=2)

    resp = client.get("/reports/inventory")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    names = {item["name"] for item in data["items"]}
    assert "Cheddar" in names
    assert failed_barcode in names
    assert data["total_value_pence"] == 300


# ---------------------------------------------------------------------------
# GET /reports/low-stock
# ---------------------------------------------------------------------------

def _seed_group(db, gid, name, minimum=0):
    db.execute(
        "INSERT INTO product_groups (id, name, minimum_quantity) VALUES (?, ?, ?)",
        (gid, name, minimum),
    )
    db.commit()


def _add_group_variant(db, gid, barcode):
    db.execute(
        "INSERT INTO group_variant_members (group_id, barcode, retailer_id) VALUES (?, ?, ?)",
        (gid, barcode, _RETAILER_ID),
    )
    db.commit()


def test_low_stock_none(client, db):
    _seed_item(db, "5000000000007", "Rice", quantity=5, minimum_quantity=2)

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    assert resp.json() == {"groups": [], "products": []}


def test_low_stock_two_section_shape(client):
    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {"groups", "products"}
    assert data == {"groups": [], "products": []}


def test_low_stock_one_product(client, db):
    _seed_item(db, "5000000000008", "Red Lentils", brand="Laila", quantity=1, minimum_quantity=3)

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    data = resp.json()
    assert data["groups"] == []
    assert len(data["products"]) == 1
    item = data["products"][0]
    assert item["barcode"] == "5000000000008"
    assert item["name"] == "Red Lentils"
    assert item["brand"] == "Laila"
    assert item["have"] == 1
    assert item["need"] == 3
    assert item["short"] == 2


def test_low_stock_excludes_exactly_at_minimum(client, db):
    _seed_item(db, "5000000000009", "Pasta", quantity=2, minimum_quantity=2)

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    assert resp.json()["products"] == []


def test_low_stock_excludes_zero_minimum(client, db):
    _seed_item(db, "5000000000010", "Herbs", quantity=0, minimum_quantity=0)

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    assert resp.json()["products"] == []


def test_low_stock_products_sorted_by_short_desc(client, db):
    _seed_item(db, "5000000000011", "Item A", quantity=2, minimum_quantity=3)
    _seed_item(db, "5000000000012", "Item B", quantity=0, minimum_quantity=3)

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    products = resp.json()["products"]
    assert len(products) == 2
    assert products[0]["name"] == "Item B"
    assert products[0]["short"] == 3
    assert products[1]["name"] == "Item A"
    assert products[1]["short"] == 1


def test_low_stock_group_below_minimum_appears(client, db):
    _seed_item(db, "5000000000013", "Bean Can", quantity=2, minimum_quantity=0)
    _seed_group(db, 1, "Beans", minimum=5)
    _add_group_variant(db, 1, "5000000000013")

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["groups"]) == 1
    g = data["groups"][0]
    assert g["group_id"] == 1
    assert g["name"] == "Beans"
    assert g["have"] == 2
    assert g["need"] == 5
    assert g["short"] == 3
    # The variant itself has minimum 0, so it does not appear in products.
    assert data["products"] == []


def test_low_stock_organisational_group_excluded(client, db):
    """A group with minimum_quantity=0 never appears regardless of stock."""
    _seed_item(db, "5000000000014", "Item", quantity=0, minimum_quantity=0)
    _seed_group(db, 1, "Org Only", minimum=0)
    _add_group_variant(db, 1, "5000000000014")

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    assert resp.json()["groups"] == []


def test_low_stock_group_and_member_both_appear(client, db):
    """A variant and a group it belongs to are evaluated independently; both can appear."""
    _seed_item(db, "5000000000015", "Shared Item", quantity=1, minimum_quantity=3)
    _seed_group(db, 1, "Group X", minimum=5)
    _add_group_variant(db, 1, "5000000000015")

    resp = client.get("/reports/low-stock")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["groups"]) == 1
    assert data["groups"][0]["short"] == 4   # 5 - 1
    assert len(data["products"]) == 1
    assert data["products"][0]["short"] == 2  # 3 - 1


# ---------------------------------------------------------------------------
# GET /reports/unresolved
# ---------------------------------------------------------------------------

_BC = "6000000000{:03d}".format  # helper: _BC(1) -> "6000000000001"


def _seed_barcode(db, barcode):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (barcode,))
    db.commit()


def _seed_pv(db, barcode, name=None, brand=None, product_quantity=None):
    db.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name, brand, product_quantity) VALUES (?, ?, ?, ?, ?)",
        (barcode, _RETAILER_ID, name, brand, product_quantity),
    )
    db.commit()


def _seed_price(db, barcode, price_pence=None, price_type='unit', product_url=None):
    db.execute(
        "INSERT INTO prices (barcode, retailer_id, price_pence, price_type, product_url) VALUES (?, ?, ?, ?, ?)",
        (barcode, _RETAILER_ID, price_pence, price_type, product_url),
    )
    db.commit()


def _seed_session(db, session_id=1):
    db.execute(
        "INSERT INTO sessions (id, type, started_at) VALUES (?, 'in', '2024-01-01T00:00:00')",
        (session_id,),
    )
    db.commit()


def _seed_session_item(db, barcode, session_id=1, info_status='pending', price_status='pending'):
    db.execute(
        """INSERT INTO session_items (session_id, barcode, retailer_id, delta, info_status, price_status, first_scanned_at)
           VALUES (?, ?, ?, 1, ?, ?, '2024-01-01T00:00:00')""",
        (session_id, barcode, _RETAILER_ID, info_status, price_status),
    )
    db.commit()


def test_unresolved_requires_auth(client_no_auth):
    resp = client_no_auth.get("/reports/unresolved")
    assert resp.status_code == 401


def test_unresolved_empty_db(client):
    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total_count": 0}


def test_unresolved_no_pv_row(client, db):
    bc = _BC(1)
    _seed_barcode(db, bc)

    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 1
    item = data["items"][0]
    assert item["barcode"] == bc
    assert item["name"]["label"] == "no_data"
    assert item["brand"]["label"] == "no_data"
    assert item["quantity"]["label"] == "no_data"
    assert item["price"]["label"] == "missing"


def test_unresolved_null_data_carry_variant_row(client, db):
    """A 1c null-data carry variant row (PV row present, all OFF fields null, no session item)
    still renders the unresolved report without crashing. Its fields label 'missing' rather than
    the no-variant 'no_data' — the I1 label shift arriving early via the orphan-carry path."""
    bc = _BC(50)
    _seed_barcode(db, bc)
    _seed_pv(db, bc)  # name/brand/product_quantity all null — the carry-row shape

    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["barcode"] == bc
    assert item["name"]["label"] == "missing"
    assert item["brand"]["label"] == "missing"
    assert item["quantity"]["label"] == "missing"
    assert item["price"]["label"] == "missing"


def test_unresolved_missing_name(client, db):
    bc = _BC(2)
    _seed_barcode(db, bc)
    _seed_pv(db, bc, name=None, brand="Heinz", product_quantity="400g")

    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["name"]["label"] == "missing"
    assert item["brand"]["label"] == "resolved"
    assert item["brand"]["value"] == "Heinz"
    assert item["quantity"]["label"] == "resolved"
    assert item["quantity"]["value"] == "400g"


def test_unresolved_missing_price(client, db):
    bc = _BC(3)
    _seed_barcode(db, bc)
    _seed_pv(db, bc, name="Beans", brand="Heinz", product_quantity="415g")
    # no prices row

    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["price"]["label"] == "missing"


def test_unresolved_per_kg(client, db):
    bc = _BC(4)
    _seed_barcode(db, bc)
    _seed_pv(db, bc, name="Loose Apples", brand="Farms", product_quantity=None)
    _seed_price(db, bc, price_pence=None, price_type='per_kg')

    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    items = resp.json()["items"]
    price_items = [i for i in items if i["barcode"] == bc]
    assert len(price_items) == 1
    assert price_items[0]["price"]["label"] == "per_kg"


def test_unresolved_fully_resolved_excluded(client, db):
    bc = _BC(5)
    _seed_barcode(db, bc)
    _seed_pv(db, bc, name="Oat Milk", brand="Oatly", product_quantity="1kg")
    _seed_price(db, bc, price_pence=150)

    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    barcodes = [i["barcode"] for i in resp.json()["items"]]
    assert bc not in barcodes


def test_unresolved_price_value_formatting(client, db):
    bc = _BC(6)
    _seed_barcode(db, bc)
    _seed_pv(db, bc, name=None, brand=None, product_quantity=None)
    _seed_price(db, bc, price_pence=110)

    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    item = next(i for i in resp.json()["items"] if i["barcode"] == bc)
    assert item["price"]["value"] == 1.1
    assert item["price"]["label"] == "resolved"


def test_unresolved_session_pending(client, db):
    bc = _BC(7)
    _seed_barcode(db, bc)
    _seed_session(db)
    _seed_session_item(db, bc, info_status='pending', price_status='pending')

    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    item = next(i for i in resp.json()["items"] if i["barcode"] == bc)
    assert item["name"]["label"] == "pending"
    assert item["brand"]["label"] == "pending"
    assert item["quantity"]["label"] == "pending"
    assert item["price"]["label"] == "pending"


def test_unresolved_session_failed_not_possible(client, db):
    bc = _BC(8)
    _seed_barcode(db, bc)
    _seed_session(db)
    _seed_session_item(db, bc, info_status='failed', price_status='not_possible')

    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    item = next(i for i in resp.json()["items"] if i["barcode"] == bc)
    assert item["name"]["label"] == "failed"
    assert item["brand"]["label"] == "failed"
    assert item["quantity"]["label"] == "failed"
    assert item["price"]["label"] == "not_attempted"


def test_unresolved_session_resolved_info_price_pending(client, db):
    bc = _BC(9)
    _seed_barcode(db, bc)
    _seed_pv(db, bc, name="Milk", brand="Arla", product_quantity="2kg")
    _seed_session(db)
    _seed_session_item(db, bc, info_status='resolved', price_status='pending')

    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    item = next(i for i in resp.json()["items"] if i["barcode"] == bc)
    assert item["name"]["label"] == "resolved"
    assert item["brand"]["label"] == "resolved"
    assert item["quantity"]["label"] == "resolved"
    assert item["price"]["label"] == "pending"


def test_unresolved_session_failed_overrides_existing_pv(client, db):
    bc = _BC(10)
    _seed_barcode(db, bc)
    _seed_pv(db, bc, name="Old Name", brand="Old Brand", product_quantity="500g")
    _seed_session(db)
    _seed_session_item(db, bc, info_status='failed', price_status='not_possible')

    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    item = next(i for i in resp.json()["items"] if i["barcode"] == bc)
    assert item["name"]["label"] == "failed"
    assert item["brand"]["label"] == "failed"
    assert item["quantity"]["label"] == "failed"


def test_unresolved_ordering_session_first(client, db):
    bc_hist = _BC(11)
    bc_sess = _BC(12)
    _seed_barcode(db, bc_hist)
    _seed_barcode(db, bc_sess)
    _seed_session(db)
    _seed_session_item(db, bc_sess, info_status='pending', price_status='pending')

    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    items = resp.json()["items"]
    barcodes = [i["barcode"] for i in items]
    assert barcodes.index(bc_sess) < barcodes.index(bc_hist)


def test_unresolved_inventory_quantity_zero_when_absent(client, db):
    bc = _BC(13)
    _seed_barcode(db, bc)
    # no inventory row

    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    item = next(i for i in resp.json()["items"] if i["barcode"] == bc)
    assert item["inventory_quantity"] == 0


def test_unresolved_inventory_quantity_from_row(client, db):
    bc = _BC(14)
    _seed_barcode(db, bc)
    _seed_pv(db, bc, name=None, brand=None, product_quantity=None)
    db.execute(
        "INSERT INTO inventory (barcode, retailer_id, quantity) VALUES (?, ?, 5)", (bc, _RETAILER_ID)
    )
    db.commit()

    resp = client.get("/reports/unresolved")
    assert resp.status_code == 200
    item = next(i for i in resp.json()["items"] if i["barcode"] == bc)
    assert item["inventory_quantity"] == 5


# ---------------------------------------------------------------------------
# off_url and price_url in inventory report
# ---------------------------------------------------------------------------

_OFF_VIEW = "https://world.openfoodfacts.org/product/{}"
_OFF_ADD  = "https://world.openfoodfacts.org/cgi/product.pl?type=edit&code={}"


def test_inventory_off_url_view_when_name_resolved(client, db):
    bc = "7000000000001"
    _seed_item(db, bc, "Oat Milk", quantity=1, price_pence=110)

    resp = client.get("/reports/inventory")
    item = resp.json()["items"][0]
    assert item["off_url"] == _OFF_VIEW.format(bc)


def test_inventory_off_url_view_when_name_is_barcode_fallback(client, db):
    bc = "7000000000002"
    _seed_off_failed_item(db, bc, quantity=1)

    resp = client.get("/reports/inventory")
    item = next(i for i in resp.json()["items"] if i["name"] == bc)
    # COALESCE gives barcode as name — still non-None, so VIEW_URL
    assert item["off_url"] == _OFF_VIEW.format(bc)


def test_inventory_price_url_none_when_no_price_url(client, db):
    bc = "7000000000003"
    _seed_item(db, bc, "Butter", quantity=1, price_pence=150)

    resp = client.get("/reports/inventory")
    item = resp.json()["items"][0]
    assert item["price_url"] is None


def test_inventory_price_url_present_when_set(client, db):
    bc = "7000000000004"
    url = "https://www.sainsburys.co.uk/gol-ui/product/butter"
    _seed_item(db, bc, "Butter", quantity=1, price_pence=150, product_url=url)

    resp = client.get("/reports/inventory")
    item = resp.json()["items"][0]
    assert item["price_url"] == url


# ---------------------------------------------------------------------------
# off_url and price_url in unresolved report
# ---------------------------------------------------------------------------

def test_unresolved_off_url_add_when_no_pv_row(client, db):
    bc = _BC(20)
    _seed_barcode(db, bc)

    resp = client.get("/reports/unresolved")
    item = next(i for i in resp.json()["items"] if i["barcode"] == bc)
    # No product_variants row → name is None → ADD URL
    assert item["off_url"] == _OFF_ADD.format(bc)


def test_unresolved_off_url_view_when_name_present(client, db):
    bc = _BC(21)
    _seed_barcode(db, bc)
    _seed_pv(db, bc, name="Milk", brand=None, product_quantity=None)

    resp = client.get("/reports/unresolved")
    item = next(i for i in resp.json()["items"] if i["barcode"] == bc)
    assert item["off_url"] == _OFF_VIEW.format(bc)


def test_unresolved_price_url_none_when_no_product_url(client, db):
    bc = _BC(22)
    _seed_barcode(db, bc)
    _seed_pv(db, bc, name=None, brand=None, product_quantity=None)
    _seed_price(db, bc, price_pence=110)

    resp = client.get("/reports/unresolved")
    item = next(i for i in resp.json()["items"] if i["barcode"] == bc)
    assert item["price_url"] is None


def test_unresolved_price_url_present_when_set(client, db):
    bc = _BC(23)
    _seed_barcode(db, bc)
    _seed_pv(db, bc, name=None, brand=None, product_quantity=None)
    url = "https://www.sainsburys.co.uk/gol-ui/product/test"
    _seed_price(db, bc, price_pence=110, product_url=url)

    resp = client.get("/reports/unresolved")
    item = next(i for i in resp.json()["items"] if i["barcode"] == bc)
    assert item["price_url"] == url
