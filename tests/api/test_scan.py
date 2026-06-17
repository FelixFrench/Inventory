"""Tests for src/api/routers/scan.py — barcode input validation (format/length)."""

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
CREATE TABLE prices (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit', product_url TEXT NULL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT PRIMARY KEY, quantity INTEGER NOT NULL DEFAULT 0, minimum_quantity INTEGER NOT NULL DEFAULT 0);
CREATE TABLE sessions (id INTEGER PRIMARY KEY, type TEXT NOT NULL, started_at TEXT NOT NULL, recovered_at TEXT);
CREATE TABLE session_items (session_id INTEGER NOT NULL, barcode TEXT NOT NULL, delta INTEGER NOT NULL, info_status TEXT NOT NULL DEFAULT 'pending', price_status TEXT NOT NULL DEFAULT 'pending', first_scanned_at TEXT NOT NULL, PRIMARY KEY (session_id, barcode));
CREATE TABLE worker_state (id INTEGER PRIMARY KEY, off_last_called_at TEXT NOT NULL);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO worker_state (id, off_last_called_at) VALUES (1, '1970-01-01T00:00:00');
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
    app.dependency_overrides[verify_api_key] = lambda: None
    app.state.sainsburys_retailer_id = 1
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_barcode_non_numeric_returns_422(client):
    resp = client.post("/scan", json={"barcode": "abc123"})
    assert resp.status_code == 422


def test_barcode_too_short_returns_422(client):
    resp = client.post("/scan", json={"barcode": "1234567"})
    assert resp.status_code == 422


def test_barcode_too_long_returns_422(client):
    resp = client.post("/scan", json={"barcode": "123456789012345"})
    assert resp.status_code == 422
