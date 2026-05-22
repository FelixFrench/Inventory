# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
