# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Scanning sessions: `POST /session`, `GET /session`, `POST /session/confirm`, `POST /session/discard`
- Session-scoped lookup state (`info_status`, `price_status`) on `session_items` replaces `pending_lookups`
- `worker_state` singleton table for OFF rate-limit anchor; persists across session deletes and reboots
- `PRAGMA busy_timeout = 250` and `PRAGMA synchronous = NORMAL` on all connections
- Crash recovery: active session resumed on FastAPI restart; `recovered_at` timestamp surfaces a UI toast
- Live scan feed at `/feed`: real-time per-item display via WebSocket, with session start/end/discard controls and a persistent status banner on all pages
- Quantity override on scanned items via `PUT /session/items/{barcode}`
- Minimum quantities management page; `PUT /products/{product_id}/minimum_quantity`
- Unresolved barcodes report at `/reports/unresolved`; `GET /reports/unresolved`
- Receipt printer output: `POST /print/inventory` and `POST /print/low-stock` send ESC/POS to EPSON TM-T88IV over TCP; printer IP configured via `PRINTER_IP` environment variable
- Product hyperlinks on inventory, feed, and unresolved pages: product names link to OpenFoodFacts; prices link to the Sainsbury's product page where available
- `product_url` column on `prices` table, populated at scrape time

### Changed
- `POST /scan` now writes to `session_items`; returns 409 `no_active_session` if no session is active
- Worker poll source changed from `pending_lookups` to `session_items`; write-back split into four explicit transactions with rowcount assertions
- `/` now redirects to `/feed` (live scan feed is the home page)

### Fixed
- Inventory and low stock reports now include items whose OpenFoodFacts lookup failed (previously silently dropped by inner join); barcode shown as fallback display name

### Removed
- `GET /mode` and `POST /mode` endpoints (deliberate breaking change)
- `pending_lookups` table (replaced by session-scoped state on `session_items`)
- `frontend/index.html` and `frontend/index.js` (V1.0 mode toggle)

## [1.0.0] - 2025-05-18

### Added
- Barcode scan-in and scan-out via Symbol LS2208 USB scanner
- Scan mode (in/out) persisted in `config` table; switchable from mobile browser UI
- Inventory counts update immediately on scan, never blocked by background lookup
- Async lookup worker: resolves product name, brand, and weight from OpenFoodFacts, then price from Sainsbury's
- New barcodes queued for async lookup automatically at scan time
- Sainsbury's price lookup via undocumented internal JSON API (no authentication required)
- Per-kg priced items stored with `price_pence = NULL` and flagged; displayed as "—" in reports
- Inventory report: quantity and unit price per product, total inventory value
- Low stock report: products where current quantity is below `minimum_quantity`
- Minimum stock levels hard-coded per product in database
- Mobile-first web UI: mode toggle, inventory report page, low stock report page
- WebSocket endpoint present at `/ws` (infrastructure only; not wired to UI)
- SQLite database stored locally on the Pi; managed via Alembic migrations on startup
- WAL mode enabled for concurrent FastAPI and worker access
- USB listener runs as a systemd service; reads directly from `/dev/input/` via `evdev`
- FastAPI server runs as a systemd service; binds to `0.0.0.0:8000`
- Async lookup worker runs as a separate always-on systemd service (`worker.service`)
- OpenFoodFacts rate limiting handled client-side: 4-second inter-request sleep with crash-recovery gap calculation
- `canonical_products` table stub present in `schema.sql` for future cross-retailer support
- `scraper_class` field on `retailers` table for future multi-retailer scraper support
- `last_updated` cache timestamps on all scraped data
- Report assembly decoupled from rendering throughout

### Security
- All API routes protected by `X-API-Key` request header; key configured via `INVENTORY_API_KEY` environment variable loaded from `config.local.env`
- `SecurityHeadersMiddleware` adds `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, and `Content-Security-Policy` headers to every response
- Frontend scripts extracted to per-page external `.js` files (`index.js`, `inventory.js`, `low_stock.js`) to satisfy CSP; inline scripts are not used
- Internal errors logged with full detail; only a generic message returned to the client
- USB listener runs without root via udev rule at `/etc/udev/rules.d/99-ffInventory-scanner.rules`
- OpenFoodFacts contact email loaded from environment; not present in repository
