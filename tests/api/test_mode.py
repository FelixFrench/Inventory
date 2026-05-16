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
CREATE TABLE barcodes (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, product_variant_id INTEGER, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (product_variant_id INTEGER PRIMARY KEY, quantity INTEGER NOT NULL DEFAULT 0, minimum_quantity INTEGER NOT NULL DEFAULT 0);
CREATE TABLE product_variants (id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_product_id INTEGER, name TEXT, brand TEXT, weight_g REAL, info_source TEXT, info_last_updated TIMESTAMP);
CREATE TABLE pending_lookups (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, queued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, status TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY (barcode, retailer_id));
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO config (key, value) VALUES ('scan_mode', 'out');
"""


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


def test_get_mode_returns_current(client):
    resp = client.get("/mode")
    assert resp.status_code == 200
    assert resp.json() == {"mode": "out"}


def test_post_mode_switches_mode(client, db):
    resp = client.post("/mode", json={"mode": "in"})
    assert resp.status_code == 200
    assert resp.json() == {"mode": "in"}

    row = db.execute("SELECT value FROM config WHERE key = 'scan_mode'").fetchone()
    assert row["value"] == "in"


def test_post_mode_invalid_value_returns_422(client):
    resp = client.post("/mode", json={"mode": "sideways"})
    assert resp.status_code == 422


def test_get_mode_after_post_reflects_change(client):
    client.post("/mode", json={"mode": "in"})
    resp = client.get("/mode")
    assert resp.status_code == 200
    assert resp.json() == {"mode": "in"}


def test_get_mode_defaults_to_out_when_missing(client, db):
    db.execute("DELETE FROM config WHERE key = 'scan_mode'")
    db.commit()

    resp = client.get("/mode")
    assert resp.status_code == 200
    assert resp.json() == {"mode": "out"}
