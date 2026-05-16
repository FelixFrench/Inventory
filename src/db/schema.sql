-- Retailers
CREATE TABLE retailers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    scraper_class TEXT NOT NULL
);

-- Product variants (retailer-specific, may be grouped later into canonical products)
CREATE TABLE product_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode TEXT NOT NULL,
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    name TEXT,
    brand TEXT,
    weight_g REAL,
    info_source TEXT,
    info_last_updated TIMESTAMP,
    price_pence INTEGER DEFAULT 0,
    price_type TEXT DEFAULT 'unit' CHECK(price_type IN ('unit', 'per_kg')),
    price_last_updated TIMESTAMP,
    quantity INTEGER NOT NULL DEFAULT 0,
    minimum_quantity INTEGER NOT NULL DEFAULT 0,
    UNIQUE(barcode, retailer_id)
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