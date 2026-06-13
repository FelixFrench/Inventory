"""Tests for Alembic migration paths — schema constraints, seed rows, V1 upgrade."""

import sqlite3
from pathlib import Path

import pytest

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, name TEXT, brand TEXT, weight_g REAL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit', product_url TEXT NULL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT PRIMARY KEY, quantity INTEGER NOT NULL DEFAULT 0, minimum_quantity INTEGER NOT NULL DEFAULT 0);
CREATE TABLE sessions (id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('in', 'out')), started_at TEXT NOT NULL, recovered_at TEXT);
CREATE TABLE session_items (session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, barcode TEXT NOT NULL REFERENCES barcodes(barcode), delta INTEGER NOT NULL CHECK(delta >= 0), info_status TEXT NOT NULL DEFAULT 'pending' CHECK(info_status IN ('pending', 'resolved', 'failed')), price_status TEXT NOT NULL DEFAULT 'pending' CHECK(price_status IN ('pending', 'resolved', 'failed', 'not_possible')), first_scanned_at TEXT NOT NULL, PRIMARY KEY (session_id, barcode));
CREATE TABLE worker_state (id INTEGER PRIMARY KEY CHECK(id = 1), off_last_called_at TEXT NOT NULL);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO worker_state (id, off_last_called_at) VALUES (1, '1970-01-01T00:00:00');
"""

_BARCODE = "5014788110140"
_SESSION_ID = 1

_V1_SCHEMA = """
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE canonical_products (id INTEGER PRIMARY KEY);
CREATE TABLE product_variants (id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_product_id INTEGER REFERENCES canonical_products(id), name TEXT, brand TEXT, weight_g REAL, info_source TEXT, info_last_updated TIMESTAMP);
CREATE TABLE barcodes (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL REFERENCES retailers(id), product_variant_id INTEGER REFERENCES product_variants(id), PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (product_variant_id INTEGER NOT NULL REFERENCES product_variants(id), retailer_id INTEGER NOT NULL REFERENCES retailers(id), price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit' CHECK(price_type IN ('unit', 'per_kg')), last_updated TIMESTAMP, PRIMARY KEY (product_variant_id, retailer_id));
CREATE TABLE inventory (product_variant_id INTEGER PRIMARY KEY REFERENCES product_variants(id), quantity INTEGER NOT NULL DEFAULT 0, minimum_quantity INTEGER NOT NULL DEFAULT 0);
CREATE TABLE scan_events (id INTEGER PRIMARY KEY AUTOINCREMENT, barcode TEXT NOT NULL, retailer_id INTEGER REFERENCES retailers(id), direction TEXT NOT NULL CHECK(direction IN ('in', 'out')), timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE pending_lookups (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL REFERENCES retailers(id), queued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'failed', 'done')), PRIMARY KEY (barcode, retailer_id));
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO config (key, value) VALUES ('scan_mode', 'out');
"""

_SCHEMA_PATH = Path(__file__).parents[2] / "src" / "db" / "initial_schema.sql"


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def _seed(conn: sqlite3.Connection, info_status: str = "pending", price_status: str = "pending"):
    conn.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (_BARCODE,))
    conn.execute(
        "INSERT INTO sessions (id, type, started_at) VALUES (?, 'in', '2026-05-27T10:00:00')",
        (_SESSION_ID,)
    )
    conn.execute(
        "INSERT INTO session_items (session_id, barcode, delta, info_status, price_status, first_scanned_at) "
        "VALUES (?, ?, 1, ?, ?, '2026-05-27T10:00:00')",
        (_SESSION_ID, _BARCODE, info_status, price_status)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Schema constraints
# ---------------------------------------------------------------------------

def test_negative_delta_raises_integrity_error(db):
    _seed(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO session_items (session_id, barcode, delta, first_scanned_at) "
            "VALUES (?, ?, -1, '2026-05-27T10:00:00')",
            (_SESSION_ID, _BARCODE)
        )


def test_invalid_info_status_raises_integrity_error(db):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES ('9999999999999')")
    db.execute(
        "INSERT INTO sessions (id, type, started_at) VALUES (2, 'in', '2026-05-27T10:00:00')"
    )
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO session_items (session_id, barcode, delta, info_status, first_scanned_at) "
            "VALUES (2, '9999999999999', 1, 'invalid_value', '2026-05-27T10:00:00')"
        )


def test_invalid_price_status_raises_integrity_error(db):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES ('8888888888888')")
    db.execute(
        "INSERT INTO sessions (id, type, started_at) VALUES (3, 'in', '2026-05-27T10:00:00')"
    )
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO session_items (session_id, barcode, delta, price_status, first_scanned_at) "
            "VALUES (3, '8888888888888', 1, 'bad_status', '2026-05-27T10:00:00')"
        )


# ---------------------------------------------------------------------------
# Worker state seed
# ---------------------------------------------------------------------------

def test_worker_state_seed_row_exists(db):
    row = db.execute("SELECT id, off_last_called_at FROM worker_state WHERE id = 1").fetchone()
    assert row is not None
    assert row["id"] == 1
    assert row["off_last_called_at"] == "1970-01-01T00:00:00"


# ---------------------------------------------------------------------------
# Migration paths
# ---------------------------------------------------------------------------

def test_migration_discards_pending_lookups_rows():
    import tempfile
    import os
    from alembic.config import Config
    from alembic import command

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        for stmt in [s.strip() for s in _V1_SCHEMA.split(";") if s.strip()]:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO barcodes (barcode, retailer_id) VALUES ('5014788110140', 1)"
        )
        conn.execute(
            "INSERT INTO pending_lookups (barcode, retailer_id, status) "
            "VALUES ('5014788110140', 1, 'pending')"
        )
        conn.commit()
        conn.close()

        alembic_cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        command.stamp(alembic_cfg, "0a3435ca1dec")
        command.upgrade(alembic_cfg, "0fcacd84ad0a")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "sessions" in tables
        assert "session_items" in tables
        assert "worker_state" in tables
        assert "pending_lookups" not in tables

        ws = conn.execute("SELECT id FROM worker_state WHERE id = 1").fetchone()
        assert ws is not None
        conn.close()
    finally:
        os.unlink(db_path)


def test_migration_fresh_db():
    import os
    import tempfile

    from alembic import command
    from alembic.config import Config

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        alembic_cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        command.upgrade(alembic_cfg, "head")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "sessions" in tables
        assert "session_items" in tables
        assert "worker_state" in tables
        assert "pending_lookups" not in tables

        ws = conn.execute(
            "SELECT id, off_last_called_at FROM worker_state WHERE id = 1"
        ).fetchone()
        assert ws is not None
        assert ws["off_last_called_at"] == "1970-01-01T00:00:00"
        conn.close()
    finally:
        os.unlink(db_path)
