import sqlite3

import pytest
from starlette.testclient import TestClient

from src.api.dependencies import get_db
from src.api.main import app

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE scan_events (id INTEGER PRIMARY KEY AUTOINCREMENT, barcode TEXT NOT NULL, retailer_id INTEGER, direction TEXT NOT NULL, timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE product_variants (id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_product_id INTEGER, name TEXT, brand TEXT, weight_g REAL, info_source TEXT, info_last_updated TIMESTAMP);
CREATE TABLE barcodes (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, product_variant_id INTEGER, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (product_variant_id INTEGER PRIMARY KEY, quantity INTEGER NOT NULL DEFAULT 0, minimum_quantity INTEGER NOT NULL DEFAULT 0);
CREATE TABLE pending_lookups (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, queued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, status TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY (barcode, retailer_id));
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO config (key, value) VALUES ('scan_mode', 'out');
"""

KNOWN_BARCODE = "1234567890123"
UNKNOWN_BARCODE = "9999999999999"


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
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_known_barcode(db, quantity=5, mode="out"):
    db.execute("UPDATE config SET value = ? WHERE key = 'scan_mode'", (mode,))
    db.execute("INSERT INTO product_variants (id) VALUES (1)")
    db.execute(
        "INSERT INTO barcodes (barcode, retailer_id, product_variant_id) VALUES (?, 1, 1)",
        (KNOWN_BARCODE,),
    )
    if quantity is not None:
        db.execute(
            "INSERT INTO inventory (product_variant_id, quantity) VALUES (1, ?)", (quantity,)
        )
    db.commit()


def test_known_barcode_out_decrements(client, db):
    _seed_known_barcode(db, quantity=5, mode="out")

    resp = client.post("/scan", json={"barcode": KNOWN_BARCODE})
    assert resp.status_code == 200
    data = resp.json()
    assert data["known"] is True
    assert data["direction"] == "out"

    qty = db.execute(
        "SELECT quantity FROM inventory WHERE product_variant_id = 1"
    ).fetchone()["quantity"]
    assert qty == 4

    event = db.execute(
        "SELECT direction FROM scan_events WHERE barcode = ?", (KNOWN_BARCODE,)
    ).fetchone()
    assert event["direction"] == "out"


def test_known_barcode_in_increments(client, db):
    _seed_known_barcode(db, quantity=5, mode="in")

    resp = client.post("/scan", json={"barcode": KNOWN_BARCODE})
    assert resp.status_code == 200
    assert resp.json()["direction"] == "in"

    qty = db.execute(
        "SELECT quantity FROM inventory WHERE product_variant_id = 1"
    ).fetchone()["quantity"]
    assert qty == 6


def test_out_at_zero_stays_zero(client, db):
    _seed_known_barcode(db, quantity=0, mode="out")

    resp = client.post("/scan", json={"barcode": KNOWN_BARCODE})
    assert resp.status_code == 200

    qty = db.execute(
        "SELECT quantity FROM inventory WHERE product_variant_id = 1"
    ).fetchone()["quantity"]
    assert qty == 0


def test_unknown_barcode_creates_pending_lookup(client, db):
    resp = client.post("/scan", json={"barcode": UNKNOWN_BARCODE})
    assert resp.status_code == 200
    assert resp.json()["known"] is False

    row = db.execute(
        "SELECT status FROM pending_lookups WHERE barcode = ?", (UNKNOWN_BARCODE,)
    ).fetchone()
    assert row is not None
    assert row["status"] == "pending"

    inv_count = db.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    assert inv_count == 0


def test_unknown_barcode_twice_no_duplicate(client, db):
    client.post("/scan", json={"barcode": UNKNOWN_BARCODE})
    client.post("/scan", json={"barcode": UNKNOWN_BARCODE})

    count = db.execute(
        "SELECT COUNT(*) FROM pending_lookups WHERE barcode = ?", (UNKNOWN_BARCODE,)
    ).fetchone()[0]
    assert count == 1

    events = db.execute(
        "SELECT COUNT(*) FROM scan_events WHERE barcode = ?", (UNKNOWN_BARCODE,)
    ).fetchone()[0]
    assert events == 2


def test_known_barcode_no_inventory_row_out(client, db):
    _seed_known_barcode(db, quantity=None, mode="out")

    resp = client.post("/scan", json={"barcode": KNOWN_BARCODE})
    assert resp.status_code == 200

    qty = db.execute(
        "SELECT quantity FROM inventory WHERE product_variant_id = 1"
    ).fetchone()["quantity"]
    assert qty == 0


def test_known_barcode_no_inventory_row_in(client, db):
    _seed_known_barcode(db, quantity=None, mode="in")

    resp = client.post("/scan", json={"barcode": KNOWN_BARCODE})
    assert resp.status_code == 200

    qty = db.execute(
        "SELECT quantity FROM inventory WHERE product_variant_id = 1"
    ).fetchone()["quantity"]
    assert qty == 1
