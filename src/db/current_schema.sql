-- current_schema.sql
--
-- Documentation: the complete database schema after all migrations have been
-- applied. Not read by any application or migration code.
--
-- Regenerate after adding a new migration:
--   sqlite3 path/to/inventory.db .schema > src/db/current_schema.sql
--
-- This file should be committed alongside each new migration so the current
-- schema is always readable without running the migrations.

CREATE TABLE retailers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    scraper_class TEXT NOT NULL
);
CREATE TABLE barcodes (
    barcode TEXT PRIMARY KEY
);
CREATE TABLE product_variants (
    barcode     TEXT    NOT NULL REFERENCES barcodes(barcode),
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    name        TEXT,
    brand       TEXT,
    weight_g    REAL,
    PRIMARY KEY (barcode, retailer_id)
);
CREATE TABLE prices (
    barcode     TEXT    NOT NULL REFERENCES barcodes(barcode),
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    price_pence INTEGER,
    price_type  TEXT NOT NULL DEFAULT 'unit'
                    CHECK(price_type IN ('unit', 'per_kg')), product_url TEXT NULL,
    PRIMARY KEY (barcode, retailer_id)
);
CREATE TABLE inventory (
    barcode          TEXT    PRIMARY KEY REFERENCES barcodes(barcode),
    quantity         INTEGER NOT NULL DEFAULT 0,
    minimum_quantity INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE sessions (
    id           INTEGER PRIMARY KEY,
    type         TEXT    NOT NULL CHECK(type IN ('in', 'out')),
    started_at   TEXT    NOT NULL,
    recovered_at TEXT
);
CREATE TABLE session_items (
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    barcode         TEXT    NOT NULL REFERENCES barcodes(barcode),
    delta           INTEGER NOT NULL CHECK(delta >= 0),
    info_status     TEXT    NOT NULL DEFAULT 'pending'
                                CHECK(info_status IN ('pending', 'resolved', 'failed')),
    price_status    TEXT    NOT NULL DEFAULT 'pending'
                                CHECK(price_status IN ('pending', 'resolved', 'failed', 'not_possible')),
    first_scanned_at TEXT   NOT NULL,
    PRIMARY KEY (session_id, barcode)
);
CREATE INDEX idx_session_items_info_pending
    ON session_items(first_scanned_at)
    WHERE info_status = 'pending';
CREATE INDEX idx_session_items_price_pending
    ON session_items(first_scanned_at)
    WHERE price_status = 'pending';
CREATE TABLE scan_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode     TEXT    NOT NULL,
    retailer_id INTEGER REFERENCES retailers(id),
    direction   TEXT    NOT NULL CHECK(direction IN ('in', 'out')),
    timestamp   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE worker_state (
    id                INTEGER PRIMARY KEY CHECK(id = 1),
    off_last_called_at TEXT NOT NULL
);
CREATE TABLE config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
