"""Tests for Alembic migration paths — schema constraints, seed rows, V1 upgrade."""

import sqlite3
from pathlib import Path

import pytest

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, name TEXT, brand TEXT, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit', product_url TEXT NULL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL REFERENCES retailers(id), quantity INTEGER NOT NULL DEFAULT 0, minimum_quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE sessions (id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('in', 'out')), started_at TEXT NOT NULL, recovered_at TEXT);
CREATE TABLE session_items (session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL REFERENCES retailers(id), delta INTEGER NOT NULL CHECK(delta >= 0), info_status TEXT NOT NULL DEFAULT 'pending' CHECK(info_status IN ('pending', 'resolved', 'failed')), price_status TEXT NOT NULL DEFAULT 'pending' CHECK(price_status IN ('pending', 'resolved', 'failed', 'not_possible')), first_scanned_at TEXT NOT NULL, PRIMARY KEY (session_id, barcode, retailer_id));
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

# Schema as of revision 3001ecf62f32 (the head before the legacy-table drop),
# including the two tables that migration removes: scan_events and config.
_PRE_DROP_SCHEMA = """
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL REFERENCES retailers(id), name TEXT, brand TEXT, product_quantity TEXT, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL REFERENCES retailers(id), price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit' CHECK(price_type IN ('unit', 'per_kg')), product_url TEXT NULL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT PRIMARY KEY REFERENCES barcodes(barcode), quantity INTEGER NOT NULL DEFAULT 0, minimum_quantity INTEGER NOT NULL DEFAULT 0);
CREATE TABLE sessions (id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('in', 'out')), started_at TEXT NOT NULL, recovered_at TEXT);
CREATE TABLE session_items (session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, barcode TEXT NOT NULL REFERENCES barcodes(barcode), delta INTEGER NOT NULL CHECK(delta >= 0), info_status TEXT NOT NULL DEFAULT 'pending' CHECK(info_status IN ('pending', 'resolved', 'failed')), price_status TEXT NOT NULL DEFAULT 'pending' CHECK(price_status IN ('pending', 'resolved', 'failed', 'not_possible')), first_scanned_at TEXT NOT NULL, PRIMARY KEY (session_id, barcode));
CREATE TABLE worker_state (id INTEGER PRIMARY KEY CHECK(id = 1), off_last_called_at TEXT NOT NULL);
CREATE TABLE scan_events (id INTEGER PRIMARY KEY AUTOINCREMENT, barcode TEXT NOT NULL, retailer_id INTEGER REFERENCES retailers(id), direction TEXT NOT NULL CHECK(direction IN ('in', 'out')), timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO worker_state (id, off_last_called_at) VALUES (1, '1970-01-01T00:00:00');
"""

# Schema as of revision 605be7ba628c (the head immediately before the 1b re-key):
# inventory is barcode-PK, session_items is (session_id, barcode)-PK, and prices carries
# two separate FKs (to barcodes and retailers). Built inline because current_schema.sql is
# now the *new* shape, so the old state cannot be reconstructed from the schema file.
_PRE_REKEY_SCHEMA = """
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL REFERENCES retailers(id), name TEXT, brand TEXT, product_quantity TEXT, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL REFERENCES retailers(id), price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit' CHECK(price_type IN ('unit', 'per_kg')), product_url TEXT NULL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT PRIMARY KEY REFERENCES barcodes(barcode), quantity INTEGER NOT NULL DEFAULT 0, minimum_quantity INTEGER NOT NULL DEFAULT 0);
CREATE TABLE sessions (id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('in', 'out')), started_at TEXT NOT NULL, recovered_at TEXT);
CREATE TABLE session_items (session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, barcode TEXT NOT NULL REFERENCES barcodes(barcode), delta INTEGER NOT NULL CHECK(delta >= 0), info_status TEXT NOT NULL DEFAULT 'pending' CHECK(info_status IN ('pending', 'resolved', 'failed')), price_status TEXT NOT NULL DEFAULT 'pending' CHECK(price_status IN ('pending', 'resolved', 'failed', 'not_possible')), first_scanned_at TEXT NOT NULL, PRIMARY KEY (session_id, barcode));
CREATE TABLE worker_state (id INTEGER PRIMARY KEY CHECK(id = 1), off_last_called_at TEXT NOT NULL);
CREATE INDEX idx_session_items_info_pending ON session_items(first_scanned_at) WHERE info_status = 'pending';
CREATE INDEX idx_session_items_price_pending ON session_items(first_scanned_at) WHERE price_status = 'pending';
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO worker_state (id, off_last_called_at) VALUES (1, '1970-01-01T00:00:00');
"""


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
        "INSERT INTO session_items "
        "(session_id, barcode, retailer_id, delta, info_status, price_status, first_scanned_at) "
        "VALUES (?, ?, 1, 1, ?, ?, '2026-05-27T10:00:00')",
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
            "INSERT INTO session_items (session_id, barcode, retailer_id, delta, first_scanned_at) "
            "VALUES (?, ?, 1, -1, '2026-05-27T10:00:00')",
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
            "INSERT INTO session_items (session_id, barcode, retailer_id, delta, info_status, first_scanned_at) "
            "VALUES (2, '9999999999999', 1, 1, 'invalid_value', '2026-05-27T10:00:00')"
        )


def test_invalid_price_status_raises_integrity_error(db):
    db.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES ('8888888888888')")
    db.execute(
        "INSERT INTO sessions (id, type, started_at) VALUES (3, 'in', '2026-05-27T10:00:00')"
    )
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO session_items (session_id, barcode, retailer_id, delta, price_status, first_scanned_at) "
            "VALUES (3, '8888888888888', 1, 1, 'bad_status', '2026-05-27T10:00:00')"
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


def test_migration_drops_legacy_scan_events_and_config():
    """605be7ba628c drops scan_events and config; downgrade recreates their structure."""
    import os
    import tempfile

    from alembic import command
    from alembic.config import Config

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        conn = sqlite3.connect(db_path)
        for stmt in [s.strip() for s in _PRE_DROP_SCHEMA.split(";") if s.strip()]:
            conn.execute(stmt)
        conn.commit()
        # Sanity: both tables exist before the migration.
        before = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "scan_events" in before
        assert "config" in before
        conn.close()

        alembic_cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        command.stamp(alembic_cfg, "3001ecf62f32")
        command.upgrade(alembic_cfg, "head")

        conn = sqlite3.connect(db_path)
        after = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "scan_events" not in after
        assert "config" not in after
        conn.close()

        # Downgrade recreates both table structures (row data not recoverable).
        command.downgrade(alembic_cfg, "3001ecf62f32")
        conn = sqlite3.connect(db_path)
        restored = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "scan_events" in restored
        assert "config" in restored
        conn.close()
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# 1b: composite re-key of inventory and session_items (revision 1a63b54732eb)
# ---------------------------------------------------------------------------

_REKEY_REV = "1a63b54732eb"
_PREV_REV = "605be7ba628c"


def _pk_columns(conn, table: str) -> list[str]:
    """Ordered PK column names for a table."""
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    pk = [r for r in rows if r[5]]  # r[5] = pk position (0 if not part of PK)
    pk.sort(key=lambda r: r[5])
    return [r[1] for r in pk]


def _fk_targets(conn, table: str) -> set[str]:
    """Set of referenced parent-table names for a table's foreign keys."""
    rows = conn.execute(f"PRAGMA foreign_key_list('{table}')").fetchall()
    return {r[2] for r in rows}  # r[2] = referenced table


def _seed_pre_rekey(conn):
    # A fully-resolved barcode: product_variants + prices + inventory + session_item.
    conn.execute("INSERT INTO barcodes (barcode) VALUES ('5000000000001')")
    conn.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name, brand, product_quantity) "
        "VALUES ('5000000000001', 1, 'Beans', 'Heinz', '415g')"
    )
    conn.execute(
        "INSERT INTO prices (barcode, retailer_id, price_pence, price_type, product_url) "
        "VALUES ('5000000000001', 1, 120, 'unit', 'https://example.test/beans')"
    )
    conn.execute(
        "INSERT INTO inventory (barcode, quantity, minimum_quantity) "
        "VALUES ('5000000000001', 7, 2)"
    )
    # A failed-OFF barcode: inventory + session_item exist WITHOUT a product_variants row
    # (proves inventory/session_items must NOT FK product_variants).
    conn.execute("INSERT INTO barcodes (barcode) VALUES ('5000000000002')")
    conn.execute(
        "INSERT INTO inventory (barcode, quantity, minimum_quantity) "
        "VALUES ('5000000000002', 3, 0)"
    )
    conn.execute("INSERT INTO sessions (id, type, started_at) VALUES (1, 'in', '2026-07-04T10:00:00')")
    conn.execute(
        "INSERT INTO session_items "
        "(session_id, barcode, delta, info_status, price_status, first_scanned_at) "
        "VALUES (1, '5000000000002', 3, 'failed', 'not_possible', '2026-07-04T10:00:00')"
    )
    conn.commit()


def _partial_index_sql(conn, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row[0] if row else None


def test_rekey_upgrade_shapes_data_and_constraints():
    """1b upgrade: composite PKs/FKs, partial indexes, backfill, fk_check clean, CHECK survives."""
    import os
    import tempfile

    from alembic import command
    from alembic.config import Config

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        conn = sqlite3.connect(db_path)
        for stmt in [s.strip() for s in _PRE_REKEY_SCHEMA.split(";") if s.strip()]:
            conn.execute(stmt)
        _seed_pre_rekey(conn)
        conn.close()

        alembic_cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        command.stamp(alembic_cfg, _PREV_REV)
        command.upgrade(alembic_cfg, "head")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Composite PKs.
        assert _pk_columns(conn, "inventory") == ["barcode", "retailer_id"]
        assert _pk_columns(conn, "session_items") == ["session_id", "barcode", "retailer_id"]
        assert _pk_columns(conn, "prices") == ["barcode", "retailer_id"]

        # FK targets: inventory/session_items -> barcodes + retailers, NOT product_variants.
        inv_fks = _fk_targets(conn, "inventory")
        assert inv_fks == {"barcodes", "retailers"}, inv_fks
        si_fks = _fk_targets(conn, "session_items")
        assert si_fks == {"sessions", "barcodes", "retailers"}, si_fks
        assert "product_variants" not in inv_fks
        assert "product_variants" not in si_fks

        # prices: single composite FK to product_variants (its only FK target now).
        price_fks = conn.execute("PRAGMA foreign_key_list('prices')").fetchall()
        assert {r["table"] for r in price_fks} == {"product_variants"}, price_fks
        # exactly one FK id, spanning both columns
        assert len({r["id"] for r in price_fks}) == 1
        assert {r["from"] for r in price_fks} == {"barcode", "retailer_id"}

        # Partial indexes recreated with verbatim WHERE clauses.
        info_sql = _partial_index_sql(conn, "idx_session_items_info_pending")
        price_sql = _partial_index_sql(conn, "idx_session_items_price_pending")
        assert info_sql is not None and "WHERE info_status = 'pending'" in info_sql
        assert price_sql is not None and "WHERE price_status = 'pending'" in price_sql

        # Backfill: every row carries retailer_id = 1 (Sainsbury's).
        assert conn.execute("SELECT COUNT(*) FROM inventory WHERE retailer_id != 1").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM session_items WHERE retailer_id != 1").fetchone()[0] == 0
        # Rows preserved.
        assert conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM session_items").fetchone()[0] == 1
        inv = conn.execute(
            "SELECT quantity, minimum_quantity FROM inventory WHERE barcode='5000000000001' AND retailer_id=1"
        ).fetchone()
        assert (inv["quantity"], inv["minimum_quantity"]) == (7, 2)

        # foreign_key_check clean.
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        # CHECK (delta >= 0) survived the rebuild.
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO session_items "
                "(session_id, barcode, retailer_id, delta, first_scanned_at) "
                "VALUES (1, '5000000000001', 1, -1, '2026-07-04T10:00:00')"
            )
        conn.close()
    finally:
        os.unlink(db_path)


def test_rekey_downgrade_roundtrip_preserves_data():
    """upgrade -> downgrade -> upgrade preserves data and restores original PKs/FKs on downgrade."""
    import os
    import tempfile

    from alembic import command
    from alembic.config import Config

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        conn = sqlite3.connect(db_path)
        for stmt in [s.strip() for s in _PRE_REKEY_SCHEMA.split(";") if s.strip()]:
            conn.execute(stmt)
        _seed_pre_rekey(conn)
        conn.close()

        alembic_cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        command.stamp(alembic_cfg, _PREV_REV)
        command.upgrade(alembic_cfg, "head")

        # Downgrade restores the barcode-only shapes and prices' two separate FKs.
        command.downgrade(alembic_cfg, _PREV_REV)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        assert _pk_columns(conn, "inventory") == ["barcode"]
        assert _pk_columns(conn, "session_items") == ["session_id", "barcode"]
        assert _fk_targets(conn, "prices") == {"barcodes", "retailers"}
        assert "retailer_id" not in {r[1] for r in conn.execute("PRAGMA table_info('inventory')").fetchall()}
        # Data preserved through the downgrade.
        assert conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM session_items").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()

        # Re-upgrade: back to composite, data still intact.
        command.upgrade(alembic_cfg, "head")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        assert _pk_columns(conn, "inventory") == ["barcode", "retailer_id"]
        assert conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM session_items").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()
    finally:
        os.unlink(db_path)


def test_rekey_idempotent_second_run_is_noop():
    """Re-running upgrade after a manual stamp is a clean no-op via the already-applied guard."""
    import os
    import tempfile

    from alembic import command
    from alembic.config import Config

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        conn = sqlite3.connect(db_path)
        for stmt in [s.strip() for s in _PRE_REKEY_SCHEMA.split(";") if s.strip()]:
            conn.execute(stmt)
        _seed_pre_rekey(conn)
        conn.close()

        alembic_cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        command.stamp(alembic_cfg, _PREV_REV)
        command.upgrade(alembic_cfg, "head")

        # Simulate a re-run after a manual stamp back to the previous revision:
        # the guard must detect the fully-applied schema and no-op.
        command.stamp(alembic_cfg, _PREV_REV)
        command.upgrade(alembic_cfg, "head")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        assert _pk_columns(conn, "inventory") == ["barcode", "retailer_id"]
        assert conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 2
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()
    finally:
        os.unlink(db_path)
