CREATE TABLE alembic_version (
	version_num VARCHAR(32) NOT NULL, 
	CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
CREATE TABLE retailers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    scraper_class TEXT NOT NULL
);
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE barcodes (
    barcode TEXT PRIMARY KEY
);
CREATE TABLE product_variants (
    barcode     TEXT    NOT NULL REFERENCES barcodes(barcode),
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    name        TEXT,
    brand       TEXT,
    product_quantity TEXT,
    minimum_quantity INTEGER NOT NULL DEFAULT 0,
    lookup_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(lookup_status IN ('pending', 'resolved', 'failed')),
    lookup_failure_count INTEGER NOT NULL DEFAULT 0 CHECK(lookup_failure_count >= 0),
    last_lookup_datetime TEXT,
    manual_refresh_requested INTEGER NOT NULL DEFAULT 0
                    CHECK(manual_refresh_requested IN (0, 1)),
    PRIMARY KEY (barcode, retailer_id)
);
CREATE TABLE prices (
    barcode     TEXT    NOT NULL,
    retailer_id INTEGER NOT NULL,
    price_pence INTEGER,
    price_type  TEXT NOT NULL DEFAULT 'unit'
                    CHECK(price_type IN ('unit', 'per_kg')), product_url TEXT NULL,
    lookup_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(lookup_status IN ('pending', 'resolved', 'failed')),
    lookup_failure_count INTEGER NOT NULL DEFAULT 0 CHECK(lookup_failure_count >= 0),
    last_lookup_datetime TEXT,
    PRIMARY KEY (barcode, retailer_id),
    FOREIGN KEY (barcode, retailer_id) REFERENCES product_variants(barcode, retailer_id)
);
CREATE TABLE inventory (
    barcode     TEXT    NOT NULL REFERENCES barcodes(barcode),
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    quantity    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (barcode, retailer_id)
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
    retailer_id     INTEGER NOT NULL REFERENCES retailers(id),
    delta           INTEGER NOT NULL CHECK(delta >= 0),
    info_status     TEXT    NOT NULL DEFAULT 'pending'
                                CHECK(info_status IN ('pending', 'resolved', 'failed')),
    price_status    TEXT    NOT NULL DEFAULT 'pending'
                                CHECK(price_status IN ('pending', 'resolved', 'failed', 'not_possible')),
    first_scanned_at TEXT   NOT NULL,
    PRIMARY KEY (session_id, barcode, retailer_id)
);
CREATE INDEX idx_session_items_info_pending
    ON session_items(first_scanned_at)
    WHERE info_status = 'pending';
CREATE INDEX idx_session_items_price_pending
    ON session_items(first_scanned_at)
    WHERE price_status = 'pending';
CREATE TABLE worker_state (
    id                INTEGER PRIMARY KEY CHECK(id = 1),
    off_last_called_at TEXT NOT NULL
);
CREATE TABLE product_groups (
    id               INTEGER PRIMARY KEY,
    name             TEXT    NOT NULL UNIQUE,
    minimum_quantity INTEGER NOT NULL DEFAULT 0 CHECK(minimum_quantity >= 0)
);
CREATE TABLE group_variant_members (
    group_id    INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE,
    barcode     TEXT    NOT NULL,
    retailer_id INTEGER NOT NULL,
    PRIMARY KEY (group_id, barcode, retailer_id),
    FOREIGN KEY (barcode, retailer_id) REFERENCES product_variants(barcode, retailer_id) ON DELETE CASCADE
);
CREATE TABLE group_group_members (
    parent_group_id INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE,
    child_group_id  INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (parent_group_id, child_group_id),
    CHECK (parent_group_id != child_group_id)
);
