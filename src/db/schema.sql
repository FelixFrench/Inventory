-- Retailers
CREATE TABLE retailers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    scraper_class TEXT NOT NULL
);

-- Product variants (retailer-specific)
CREATE TABLE product_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    brand TEXT,
    weight_g REAL,
    info_source TEXT,
    info_last_updated TIMESTAMP
);

-- Barcode to product variant mapping
CREATE TABLE barcodes (
    barcode TEXT NOT NULL,
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    product_variant_id INTEGER REFERENCES product_variants(id),
    PRIMARY KEY (barcode, retailer_id)
);

-- Prices (one row per product variant / retailer combo)
CREATE TABLE prices (
    product_variant_id INTEGER NOT NULL REFERENCES product_variants(id),
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    price_pence INTEGER NOT NULL DEFAULT 0,
    price_type TEXT NOT NULL DEFAULT 'unit' CHECK(price_type IN ('unit', 'per_kg')),
    last_updated TIMESTAMP,
    PRIMARY KEY (product_variant_id, retailer_id)
);

-- Inventory (one row per product variant)
CREATE TABLE inventory (
    product_variant_id INTEGER PRIMARY KEY REFERENCES product_variants(id),
    quantity INTEGER NOT NULL DEFAULT 0,
    minimum_quantity INTEGER NOT NULL DEFAULT 0
);

-- Scan events (full history)
CREATE TABLE scan_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode TEXT NOT NULL,
    retailer_id INTEGER REFERENCES retailers(id),
    direction TEXT NOT NULL CHECK(direction IN ('in', 'out')),
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Pending async lookup jobs
CREATE TABLE pending_lookups (
    barcode TEXT NOT NULL,
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    queued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'failed', 'done')),
    PRIMARY KEY (barcode, retailer_id)
);
