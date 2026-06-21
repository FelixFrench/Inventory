# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Multi-retailer price sources (e.g. Lidl, Tesco) via additional scraper modules implementing the Sainsbury's scraper interface.
- Product groups: a layer above retailer-specific variants so equivalent products (e.g. own-brand and branded baked beans) count toward a single minimum-stock level (new `product_groups` table).
- Per-kg pricing support for items currently stored with `price_pence = NULL` and flagged as per-kg.
- Periodic price-cache refresh worker to keep prices current and to retry previously-failed lookups — this also resolves transient OpenFoodFacts / Sainsbury's errors that are currently recorded as permanent `failed` status.
- Ambiguous barcode resolution: prompt the user to resolve a barcode that maps to different products across retailers (relevant once multi-retailer support lands).

## [2.1.0] - 2026-06-21

### Added
- Web app manifest (`frontend/manifest.json`) and home-screen icons, linked from every page, so the app can be added to a device home screen. Standalone (app-like) display and the automatic install prompt are only available where the app is served over HTTPS; over plain HTTP the manifest is ignored by browsers and this is a known limitation rather than a defect.
- Favicon link (`/icons/icon-192.png`) on every page, resolving the browser favicon `404`.
- `scripts/clear_products.sh`, a development-only reset tool that stops the services, clears cached product data not referenced by current inventory (prices, product variants, barcodes) along with any zero-quantity inventory rows, then restarts the services.

### Changed
- Product packaging quantity is now stored verbatim as text in `product_variants.product_quantity`, replacing the grams-only `weight_g` numeric column. Non-gram units are preserved as supplied by OpenFoodFacts (e.g. `"500ml"`, `"1.5kg"`) instead of being coerced to grams. The value is captured once at lookup time. The API response field that carries it is unchanged in name and shape (still a string).
- Sainsbury's price-search queries now include the product's actual packaging unit rather than assuming grams, improving match rates for items measured by volume.

### Fixed
- Time-of-check/time-of-use race in `POST /session/confirm`: the pending-lookup gate is now re-checked inside the write transaction (with an explicit rollback before the `409`), closing a window where a concurrent `POST /scan` could be flushed unresolved. The `409` response shape is unchanged.
- Sainsbury's price lookups that failed when the OpenFoodFacts unit and the Sainsbury's unit disagreed (e.g. `500g` vs `500ml`).
- The `would_go_negative` branch of session confirm now rolls back its transaction explicitly before returning `409`, consistent with the pending-lookup gate.

### Removed
- `weight_g` column from `product_variants`, superseded by `product_quantity` (applied by migration).
- `frontend/spike_c_mockup.html`, an obsolete design mockup.

### Security
- The OpenFoodFacts-sourced packaging-quantity value is now length-capped (64 characters) before it is written to the database, bounding an anomalous upstream response from writing an unbounded value.

## [2.0.0] - 2026-06-17

### Added
- Scanning sessions replace the V1.0 binary in/out scan mode: `POST /session`, `GET /session`, `POST /session/confirm`, `POST /session/discard`. A session is started explicitly as `in` or `out`; scanned items accumulate as unsigned deltas and are applied to inventory on confirm.
- Live scan feed at `/feed.html` (now the home page): real-time per-item display over WebSocket, with session start / end / discard controls and a persistent status banner on every page.
- Quantity override on scanned items via `PUT /session/items/{barcode}` — adjust the count for a barcode within the active session (e.g. to undo a misscan or add multiples from one scan).
- Minimum quantities are now editable from the web UI via `PUT /products/{barcode}/minimum_quantity`, replacing hard-coded database values.
- Unresolved barcodes report at `/reports/unresolved` (`GET /reports/unresolved`): lists barcodes with incomplete product data and per-field resolution status.
- Receipt printer output: `POST /print/inventory` and `POST /print/low-stock` send ESC/POS to an EPSON TM-T88IV thermal printer over TCP; printer IP configured via the `PRINTER_IP` environment variable.
- Product hyperlinks on the inventory, feed, and unresolved pages: product names link to OpenFoodFacts (view URL normally, add/edit URL when the OpenFoodFacts lookup failed); prices link to the Sainsbury's product page where available.
- Interactive API documentation: Swagger UI at `/docs` and the OpenAPI schema at `/openapi.json`. `/docs` is gated behind the `X-API-Key` header or a scoped session cookie; `/openapi.json` is unauthenticated (API structure only, no inventory data).
- Crash recovery: an interrupted session is resumed automatically on FastAPI restart; a `recovered_at` timestamp surfaces a time-bounded recovery toast in the UI.
- Session-scoped lookup state (`info_status`, `price_status`) on the new `session_items` table.
- `worker_state` singleton table holding the OpenFoodFacts rate-limit anchor; persists across session deletes and Pi reboots.
- `product_url` column on the `prices` table, populated at scrape time.
- `PRAGMA busy_timeout = 250` and `PRAGMA synchronous = NORMAL` on all database connections.

### Changed
- `POST /scan` now writes to the active session's `session_items` and returns `409 no_active_session` when no session is active.
- The background lookup worker now polls `session_items` instead of `pending_lookups`; inventory write-back on confirm is split into explicit transactions with rowcount assertions.
- `/` now redirects to `/feed.html`.
- API contract: negative `delta` and `minimum_quantity` values are now rejected at the schema layer with `422 Unprocessable Entity` (previously `400 Bad Request`). This is a minor breaking change for any client that relied on the `400` status.

### Removed
- `GET /mode` and `POST /mode` endpoints (deliberate breaking change — superseded by scanning sessions).
- `pending_lookups` table (replaced by session-scoped state on `session_items`).
- `frontend/index.html` and `frontend/index.js` (the V1.0 mode toggle).
- `canonical_products` stub table — the unused cross-retailer placeholder that shipped in V1.0; a `product_groups` table will replace it when product grouping is implemented (see [Unreleased] → Planned).

### Fixed
- Inventory and low stock reports now include items whose OpenFoodFacts lookup failed (previously silently dropped by an inner join); the barcode is shown as a fallback display name.
- The worker poll loop now has a top-level exception guard, so a transient error in one iteration no longer terminates the worker process.
- Error-response shape and `503` handling are now consistent across the reports and products routers.
- The fresh-install database migration chain no longer fails with a duplicate `product_url` column.
- Frontend fetch failures (live-feed delta updates, docs navigation) are no longer swallowed silently; a failed delta update reverts the optimistic UI change instead of leaving an unpersisted count on screen.

### Security
- API key comparison is now constant-time (`secrets.compare_digest`), removing a timing side-channel.
- Hyperlink rendering validates the URL scheme before output, preventing `javascript:`-scheme injection via a stored product URL.
- Non-negative schema validation (`Field(ge=0)`) added on `delta` and `minimum_quantity`.
- Removed the unused `httpx2` dependency.

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
