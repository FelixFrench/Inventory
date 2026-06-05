-- Inventory V1.1.0 Schema (Phase 1)
-- SQLite. WAL mode is enabled at connection time in db.py, not here.

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------

CREATE TABLE retailers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    scraper_class TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Product data (barcode-keyed, Phase 1)
-- ---------------------------------------------------------------------------

-- Barcode registry. Populated at scan time before session_items upsert (FK dependency).
CREATE TABLE barcodes (
    barcode TEXT PRIMARY KEY
);

-- One row per barcode+retailer combination. Populated by the worker on OFF resolution.
CREATE TABLE product_variants (
    barcode     TEXT    NOT NULL REFERENCES barcodes(barcode),
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    name        TEXT,
    brand       TEXT,
    weight_g    REAL,
    PRIMARY KEY (barcode, retailer_id)
);

-- One price row per barcode+retailer. Populated by the worker on Sainsbury's resolution.
CREATE TABLE prices (
    barcode     TEXT    NOT NULL REFERENCES barcodes(barcode),
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    price_pence INTEGER,
    price_type  TEXT NOT NULL DEFAULT 'unit'
                    CHECK(price_type IN ('unit', 'per_kg')),
    product_url TEXT NULL,
    PRIMARY KEY (barcode, retailer_id)
);

-- ---------------------------------------------------------------------------
-- Inventory
-- ---------------------------------------------------------------------------

-- One row per barcode. Updated by session confirm.
CREATE TABLE inventory (
    barcode          TEXT    PRIMARY KEY REFERENCES barcodes(barcode),
    quantity         INTEGER NOT NULL DEFAULT 0,
    minimum_quantity INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Scanning sessions (Phase 1)
-- ---------------------------------------------------------------------------

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

-- ---------------------------------------------------------------------------
-- Operational tables
-- ---------------------------------------------------------------------------

-- Full scan history (written by V1.0 scan.py; no longer written in Phase 1).
CREATE TABLE scan_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode     TEXT    NOT NULL,
    retailer_id INTEGER REFERENCES retailers(id),
    direction   TEXT    NOT NULL CHECK(direction IN ('in', 'out')),
    timestamp   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Worker rate-limit anchor. Singleton row (id=1) seeded by migration.
CREATE TABLE worker_state (
    id                INTEGER PRIMARY KEY CHECK(id = 1),
    off_last_called_at TEXT NOT NULL
);

-- Key/value store for runtime configuration.
CREATE TABLE config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Seed data
-- ---------------------------------------------------------------------------

INSERT INTO retailers (name, scraper_class)
VALUES ('Sainsbury''s', 'SainsburysProvider');

INSERT INTO worker_state (id, off_last_called_at)
VALUES (1, '1970-01-01T00:00:00');
