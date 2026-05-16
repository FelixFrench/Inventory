-- Inventory V1.0 Schema
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
-- Product data
-- ---------------------------------------------------------------------------

-- One row per retailer-specific product variant.
-- canonical_product_id is nullable; it will be populated when the canonical
-- products layer is introduced in a future version.
CREATE TABLE product_variants (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_product_id INTEGER REFERENCES canonical_products(id),
    name                 TEXT,
    brand                TEXT,
    weight_g             REAL,
    info_source          TEXT,
    info_last_updated    TIMESTAMP
);

-- Barcode → product variant, scoped per retailer.
-- Compound PK allows the same barcode to map to different variants at
-- different retailers (ambiguous barcode handling, future versions).
CREATE TABLE barcodes (
    barcode            TEXT    NOT NULL,
    retailer_id        INTEGER NOT NULL REFERENCES retailers(id),
    product_variant_id INTEGER REFERENCES product_variants(id),
    PRIMARY KEY (barcode, retailer_id)
);

-- One price row per variant/retailer combination.
-- price_pence is nullable: NULL means price not yet resolved.
-- per_kg items cannot contribute to inventory value totals until weight is known.
CREATE TABLE prices (
    product_variant_id INTEGER NOT NULL REFERENCES product_variants(id),
    retailer_id        INTEGER NOT NULL REFERENCES retailers(id),
    price_pence        INTEGER,
    price_type         TEXT NOT NULL DEFAULT 'unit'
                           CHECK(price_type IN ('unit', 'per_kg')),
    last_updated       TIMESTAMP,
    PRIMARY KEY (product_variant_id, retailer_id)
);

-- ---------------------------------------------------------------------------
-- Inventory
-- ---------------------------------------------------------------------------

-- One row per product variant. minimum_quantity is hard-coded for V1;
-- it will move to canonical_products when that layer is introduced.
CREATE TABLE inventory (
    product_variant_id INTEGER PRIMARY KEY REFERENCES product_variants(id),
    quantity           INTEGER NOT NULL DEFAULT 0,
    minimum_quantity   INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Operational tables
-- ---------------------------------------------------------------------------

-- Full scan history. retailer_id recorded at scan time so history is correct
-- even if barcode→variant mapping changes later.
CREATE TABLE scan_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode     TEXT    NOT NULL,
    retailer_id INTEGER REFERENCES retailers(id),
    direction   TEXT    NOT NULL CHECK(direction IN ('in', 'out')),
    timestamp   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Queue for async product info + price lookups.
CREATE TABLE pending_lookups (
    barcode     TEXT    NOT NULL,
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    queued_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'failed', 'done')),
    PRIMARY KEY (barcode, retailer_id)
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

INSERT INTO config (key, value)
VALUES ('scan_mode', 'out');
