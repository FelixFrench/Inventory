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
CREATE TABLE product_variants (barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL REFERENCES retailers(id), name TEXT, brand TEXT, product_quantity TEXT, minimum_quantity INTEGER NOT NULL DEFAULT 0, lookup_status TEXT NOT NULL DEFAULT 'pending' CHECK(lookup_status IN ('pending', 'resolved', 'failed')), lookup_failure_count INTEGER NOT NULL DEFAULT 0 CHECK(lookup_failure_count >= 0), last_lookup_datetime TEXT, manual_refresh_requested INTEGER NOT NULL DEFAULT 0 CHECK(manual_refresh_requested IN (0, 1)), PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit', product_url TEXT NULL, lookup_status TEXT NOT NULL DEFAULT 'pending' CHECK(lookup_status IN ('pending', 'resolved', 'failed')), lookup_failure_count INTEGER NOT NULL DEFAULT 0 CHECK(lookup_failure_count >= 0), last_lookup_datetime TEXT, PRIMARY KEY (barcode, retailer_id));
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


def test_put_minimum_creates_variant_with_pending_lookup_status(client, db):
    """The set-minimum upsert on a fresh barcode must leave lookup_status at its 'pending'
    default (the row was never OFF-resolved) — it must not name the durable columns."""
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    resp = client.put("/products/5014788110140/1/minimum_quantity", json={"minimum_quantity": 4})
    assert resp.status_code == 200
    row = db.execute(
        "SELECT lookup_status, lookup_failure_count, last_lookup_datetime FROM product_variants "
        "WHERE barcode = '5014788110140' AND retailer_id = 1"
    ).fetchone()
    assert row["lookup_status"] == "pending"
    assert row["lookup_failure_count"] == 0
    assert row["last_lookup_datetime"] is None


def test_put_minimum_does_not_reset_lookup_status_on_conflict(client, db):
    """PUT onto an already-resolved variant updates only minimum_quantity; lookup_status and the
    other durable columns are preserved (ON CONFLICT DO UPDATE must not touch them)."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_variants (barcode, retailer_id, name, minimum_quantity, lookup_status, lookup_failure_count, last_lookup_datetime)
            VALUES ('5014788110140', 1, 'Beans', 1, 'resolved', 0, '2026-07-20T09:00:00+00:00');
    """)
    resp = client.put("/products/5014788110140/1/minimum_quantity", json={"minimum_quantity": 6})
    assert resp.status_code == 200
    row = db.execute(
        "SELECT minimum_quantity, lookup_status, last_lookup_datetime FROM product_variants "
        "WHERE barcode = '5014788110140' AND retailer_id = 1"
    ).fetchone()
    assert row["minimum_quantity"] == 6
    assert row["lookup_status"] == "resolved"  # preserved
    assert row["last_lookup_datetime"] == "2026-07-20T09:00:00+00:00"


def test_product_add_group_creates_variant_with_pending_lookup_status(client, db):
    """The group-membership add upsert (ON CONFLICT DO NOTHING) leaves lookup_status='pending'
    on the null-data carry row it creates."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_groups (id, name) VALUES (1, 'G');
    """)
    resp = client.post("/products/5014788110140/1/groups", json={"group_id": 1})
    assert resp.status_code == 200
    row = db.execute(
        "SELECT lookup_status FROM product_variants WHERE barcode='5014788110140' AND retailer_id=1"
    ).fetchone()
    assert row["lookup_status"] == "pending"


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


def test_product_detail_bare_barcode_no_variant_returns_200(client, db):
    """The unresolved report links to this page for a barcode present in `barcodes` but with
    zero product_variants row (a genuinely un-looked-up barcode). It must return 200 with
    null-data fields, never 404, so that link is not dead."""
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    # Precondition: no product_variants row exists for this barcode.
    assert db.execute(
        "SELECT 1 FROM product_variants WHERE barcode = '5014788110140'"
    ).fetchone() is None

    resp = client.get("/products/5014788110140/1")
    assert resp.status_code == 200
    d = resp.json()
    assert d["barcode"] == "5014788110140"
    assert d["name"] is None and d["brand"] is None and d["product_quantity"] is None
    assert d["price_pence"] is None
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


def test_product_remove_group_unknown_barcode_404(client, db):
    """The barcode arm of the existence rule, mirroring the group arm above.

    Only the EDGE is idempotent: an unknown barcode is a client mistake (404), in contrast
    with the valid-but-non-member no-op directly above.
    """
    db.execute("INSERT INTO product_groups (id, name) VALUES (1, 'G')")
    db.commit()
    resp = client.delete("/products/99999999/1/groups/1")
    assert resp.status_code == 404
    assert resp.json() == {"error": "barcode_not_found"}


def test_product_add_group_membership_requires_auth(client_no_auth, db):
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_groups (id, name) VALUES (1, 'G');
    """)
    resp = client_no_auth.post("/products/5014788110140/1/groups", json={"group_id": 1})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /products/{barcode}/{retailer_id}/refresh — manual refresh (3c)
# ---------------------------------------------------------------------------

def test_refresh_returns_202_and_marks(client, db):
    """202 for an in-system barcode with an existing variant; sets manual_refresh_requested=1."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_variants (barcode, retailer_id, name) VALUES ('5014788110140', 1, 'Baked Beans');
    """)
    resp = client.post("/products/5014788110140/1/refresh")
    assert resp.status_code == 202
    assert resp.json() == {"status": "queued", "barcode": "5014788110140", "retailer_id": 1}
    marker = db.execute(
        "SELECT manual_refresh_requested FROM product_variants WHERE barcode='5014788110140' AND retailer_id=1"
    ).fetchone()[0]
    assert marker == 1


def test_refresh_upserts_null_data_variant(client, db):
    """A barcode present in barcodes but with no product_variants row gets a null-data pending row
    with marker=1, touching nothing else."""
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    resp = client.post("/products/5014788110140/1/refresh")
    assert resp.status_code == 202
    row = db.execute(
        "SELECT name, brand, product_quantity, minimum_quantity, lookup_status, "
        "lookup_failure_count, last_lookup_datetime, manual_refresh_requested "
        "FROM product_variants WHERE barcode='5014788110140' AND retailer_id=1"
    ).fetchone()
    assert row["name"] is None
    assert row["brand"] is None
    assert row["product_quantity"] is None
    assert row["minimum_quantity"] == 0
    assert row["lookup_status"] == "pending"
    assert row["lookup_failure_count"] == 0
    assert row["last_lookup_datetime"] is None
    assert row["manual_refresh_requested"] == 1


def test_refresh_conflict_preserves_all_other_columns(client, db):
    """On an existing variant, refresh sets ONLY the marker; every other column is unchanged."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_variants
            (barcode, retailer_id, name, brand, product_quantity, minimum_quantity,
             lookup_status, lookup_failure_count, last_lookup_datetime, manual_refresh_requested)
            VALUES ('5014788110140', 1, 'Baked Beans', 'Heinz', '415g', 4,
                    'failed', 3, '2026-07-01T00:00:00+00:00', 0);
    """)
    db.commit()
    resp = client.post("/products/5014788110140/1/refresh")
    assert resp.status_code == 202
    row = db.execute(
        "SELECT name, brand, product_quantity, minimum_quantity, lookup_status, "
        "lookup_failure_count, last_lookup_datetime, manual_refresh_requested "
        "FROM product_variants WHERE barcode='5014788110140' AND retailer_id=1"
    ).fetchone()
    assert row["name"] == "Baked Beans"
    assert row["brand"] == "Heinz"
    assert row["product_quantity"] == "415g"
    assert row["minimum_quantity"] == 4
    assert row["lookup_status"] == "failed"
    assert row["lookup_failure_count"] == 3
    assert row["last_lookup_datetime"] == "2026-07-01T00:00:00+00:00"
    assert row["manual_refresh_requested"] == 1


def test_refresh_is_idempotent(client, db):
    """A second call before the worker processes leaves the marker at 1 (not 2, not reset)."""
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    assert client.post("/products/5014788110140/1/refresh").status_code == 202
    assert client.post("/products/5014788110140/1/refresh").status_code == 202
    marker = db.execute(
        "SELECT manual_refresh_requested FROM product_variants WHERE barcode='5014788110140' AND retailer_id=1"
    ).fetchone()[0]
    assert marker == 1


def test_refresh_barcode_not_found(client, db):
    """A barcode absent from barcodes returns 404 barcode_not_found (FK IntegrityError, rolled back)."""
    resp = client.post("/products/9999999999999/1/refresh")
    assert resp.status_code == 404
    assert resp.json() == {"error": "barcode_not_found"}
    assert db.execute("SELECT 1 FROM product_variants").fetchone() is None


def test_refresh_retailer_not_found(client, db):
    """A nonexistent retailer_id returns 404 retailer_not_found (guarded before the upsert)."""
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    resp = client.post("/products/5014788110140/999/refresh")
    assert resp.status_code == 404
    assert resp.json() == {"error": "retailer_not_found"}
    assert db.execute("SELECT 1 FROM product_variants WHERE retailer_id=999").fetchone() is None


def test_refresh_requires_auth(client_no_auth, db):
    """Returns 401 when no API key header is provided."""
    db.executescript("""
        INSERT INTO barcodes VALUES ('5014788110140');
        INSERT INTO product_variants (barcode, retailer_id) VALUES ('5014788110140', 1);
    """)
    resp = client_no_auth.post("/products/5014788110140/1/refresh")
    assert resp.status_code == 401
    # The endpoint did nothing (auth rejected before the handler): marker still 0.
    marker = db.execute(
        "SELECT manual_refresh_requested FROM product_variants WHERE barcode='5014788110140' AND retailer_id=1"
    ).fetchone()[0]
    assert marker == 0


def test_refresh_makes_no_network_call(client, db):
    """The endpoint performs no OFF/Sainsbury's/network call — only a DB write. The products router
    imports no OFF/scraper client, so any outbound call would have to go through the worker modules;
    patching them to explode proves the request path never touches them."""
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    with patch("src.worker.off.lookup_barcode", side_effect=AssertionError("OFF must not be called")), \
         patch("src.worker.sainsburys.get_price", side_effect=AssertionError("price must not be called")):
        resp = client.post("/products/5014788110140/1/refresh")
    assert resp.status_code == 202


def test_refresh_makes_no_outbound_http_at_the_transport_layer(client, db):
    """The same invariant one level lower: no HTTP leaves the process at all.

    The function-level patch above cannot see a raw requests call made inside the router, so
    block the transport instead. Both worker clients go through requests (off.py / sainsburys.py
    use requests.get), while starlette's TestClient is an httpx.Client — the two stacks are
    disjoint, so this patch cannot intercept the test's own POST.
    """
    db.execute("INSERT INTO barcodes VALUES ('5014788110140')")
    db.commit()
    with patch(
        "requests.adapters.HTTPAdapter.send",
        side_effect=AssertionError("no outbound HTTP may leave the refresh endpoint"),
    ):
        resp = client.post("/products/5014788110140/1/refresh")

    assert resp.status_code == 202
    marker = db.execute(
        "SELECT manual_refresh_requested FROM product_variants "
        "WHERE barcode='5014788110140' AND retailer_id=1"
    ).fetchone()[0]
    assert marker == 1


def test_transport_patch_would_actually_catch_a_requests_call():
    """Negative control for the test above: prove the patch really blocks requests.get.

    Without this, a patch target that silently stopped matching would leave the no-network
    assertion passing vacuously.
    """
    import requests

    with patch(
        "requests.adapters.HTTPAdapter.send",
        side_effect=AssertionError("blocked"),
    ):
        with pytest.raises(AssertionError, match="blocked"):
            requests.get("https://world.openfoodfacts.org/api/v2/product/1.json", timeout=1)
