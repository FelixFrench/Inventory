# Inventory

A barcode-driven grocery inventory system running on a Raspberry Pi 4B. Open a
scanning session, scan items in or out with a USB barcode scanner, review the
batch on a live feed, then confirm it to inventory. Product names, brands, and
prices are resolved automatically in the background.

Version history is in [CHANGELOG.md](CHANGELOG.md); releases are tagged in git.

---

## Overview

- Scanning is **session-based**: start an *in* or *out* session, scan a batch of
  items, then confirm the whole batch to inventory (or discard it)
- A **live feed** shows scanned items in real time as they arrive, including
  background-lookup status
- New barcodes are resolved automatically via OpenFoodFacts, then priced via the
  Sainsbury's internal API — lookups never block scanning
- Inventory, low-stock, and unresolved-barcode reports are available from any
  device on the LAN via a mobile-first web UI, and can be sent to a receipt printer
- Minimum stock levels are editable from the web UI
- All data is stored locally on the Pi in SQLite

---

## How it works

### The session lifecycle

The central concept is the **session** — a single stock-take in one direction:

1. **Start** a session as `in` (receiving stock) or `out` (using stock). Only one
   session is active at a time.
2. **Scan** items. Each scan records an *unsigned* count against its barcode in
   the active session — nothing is written to inventory yet. Scanning with no
   active session is rejected (`409 no_active_session`).
3. **Review** on the live feed as items arrive, adjusting counts if needed (for
   example to undo a misscan).
4. **Confirm** to apply the session to inventory — counts are *added* for an `in`
   session and *subtracted* for an `out` session — or **discard** to throw the
   session away with no inventory change.

Confirm is held back until every item's background lookup has finished, and (for
`out` sessions) it's refused if it would take any item below zero. If the API
restarts mid-session, the in-progress session is recovered on startup and left
active for you to finish — it is never auto-confirmed or discarded — and the UI
shows a brief recovery notice.

### Services

Three systemd services run on the Pi:

| Service | Role |
|---------|------|
| `fastapi` | REST API (sessions, scans, reports, printing, product edits), the live-feed WebSocket, and serving the frontend |
| `listener` | Reads the USB scanner and POSTs barcodes to FastAPI |
| `worker` | Resolves unresolved barcodes from the active session — product info from OpenFoodFacts, then price from Sainsbury's |

### Data flow on a scan

1. The LS2208 scans a barcode → `listener` detects the keystrokes via `/dev/input/`
2. `listener` POSTs `{ "barcode": "..." }` to `http://127.0.0.1:8000/scan`
3. FastAPI appends the scan to the active session as an unsigned delta in
   `session_items` (or returns `409 no_active_session` if none is open), and pushes
   the update to any connected live-feed clients over the WebSocket
4. If the barcode hasn't been resolved before, the `worker` picks it up from
   `session_items` and resolves it in two phases — OpenFoodFacts for product info,
   then Sainsbury's for price — without blocking scanning
5. On **confirm**, the session's deltas are applied to inventory in explicit,
   row-count-checked transactions, and the session is cleared; on **discard**, the
   session is dropped with no inventory change

---

## Requirements

- Raspberry Pi 4B (or similar Linux host)
- Symbol LS2208 barcode scanner (USB HID; matched by USB vendor `0x05e0` /
  product `0x1200`)
- Python 3.11+
- *Optional:* an EPSON TM-T88IV network thermal printer (ESC/POS over TCP port
  9100), for printing reports

---

## Setup

### 1. Clone and create virtualenv

```bash
git clone <repo-url> ~/repos/Inventory
cd ~/repos/Inventory
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> On non-Pi development hosts (x86_64), the `evdev` dependency — used only by the
> scanner listener — is skipped automatically; it installs only on aarch64 Linux.

### 2. Configure environment

Create `config.local.env` (already in `.gitignore`):

```
OFF_CONTACT_EMAIL=your@email.com
INVENTORY_API_KEY=choose-a-strong-random-secret

# Receipt printer (optional, EPSON TM-T88IV over Ethernet)
# PRINTER_IP=192.168.1.50
```

`OFF_CONTACT_EMAIL` is used to construct the OpenFoodFacts `User-Agent` header, as
required by the OFF API terms; the worker won't start without it.

`INVENTORY_API_KEY` is the shared secret that protects the API. The frontend pages
and the listener must present this key in the `X-API-Key` request header; the API
won't start without it.

`PRINTER_IP` is optional. Leave it commented out unless you have the printer — the
rest of the system runs normally without it, and the print endpoints simply return
a "printer not configured" error.

### 2a. Configure the frontend API key

```bash
cp frontend/config.example.js frontend/config.js
# then edit frontend/config.js and set API_KEY to match INVENTORY_API_KEY
```

`frontend/config.js` is gitignored and must be created manually on each
installation.

### 3. Initialise the database

```bash
alembic upgrade head
```

This creates `inventory.db` with WAL mode enabled, applies all migrations, and
seeds the `retailers` table (Sainsbury's) and the `worker_state` rate-limit
anchor. The API also runs migrations automatically on startup, so this step is
optional if you start the service first — but it's useful to run by hand on a
fresh install.

### 4. Scanner permissions (udev rule)

```bash
sudo cp systemd/99-inventory-scanner.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo usermod -aG input felix   # replace felix with your username
```

This grants the `input` group read access to the scanner device so the listener
can run without root.

### 5. Install and start systemd services

```bash
# The units run as user `felix` from /home/felix/repos/Inventory — edit both to
# match your username and clone path first.
sudo cp systemd/fastapi.service /etc/systemd/system/
sudo cp systemd/listener.service /etc/systemd/system/
sudo cp systemd/worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fastapi listener worker
```

The web UI is then available at `http://<pi-ip>:8000` from any device on the LAN.

### 6. (Optional) Enable the API docs UI

The Swagger UI assets are not committed. If you want the interactive docs at
`/docs`, fetch them once:

```bash
scripts/download_swagger_assets.sh
```

The rest of the API (including `/openapi.json`) works without this.

---

## Usage

### Run a stock-take

The home page (`/feed.html`) is the live feed and session control.

1. Start a session in the direction you want — *in* to add stock, *out* to use it.
2. Scan items; they appear on the feed as they arrive, with lookup status.
3. Adjust a count if needed, then **confirm** to commit the batch to inventory, or
   **discard** to cancel it.

The same actions are available via the API. The session direction field is named
`type`:

```bash
# Start an "in" session
curl -X POST http://localhost:8000/session \
     -H "Content-Type: application/json" \
     -H "X-API-Key: <your-key>" \
     -d '{"type":"in"}'
```

Then `POST /session/confirm` or `POST /session/discard` (no body), and
`PUT /session/items/{barcode}` with `{"delta": <n>}` to override a count. For exact
request/response shapes across all endpoints, use the interactive docs (below)
rather than hard-coding from here.

### Reports

In the browser:

- `/feed.html` — live scan feed and session control (home page)
- `/inventory.html` — full inventory with quantities and prices
- `/low_stock.html` — items below their minimum quantity
- `/unresolved.html` — barcodes whose product data is incomplete, with per-field
  resolution status
- `/min_quantities.html` — view and edit minimum stock levels

Or via the API:

```
GET /reports/inventory
GET /reports/low-stock
GET /reports/unresolved
```

Items with unresolved prices display as `—`. Product names and prices in the
reports link out to OpenFoodFacts and the Sainsbury's product page where available.

### Print a report

With a printer configured (`PRINTER_IP` set), send a report to the receipt printer:

```
POST /print/inventory
POST /print/low-stock
```

### Edit a minimum stock level

From the `/min_quantities.html` page, or via the API:

```
GET /products/minimum-quantities
PUT /products/{barcode}/minimum_quantity      # body: {"minimum_quantity": <n>}
```

### API documentation

The app serves its own API docs:

- `/docs` — Swagger UI (gated; authenticate via `POST /docs-login`, or present the
  `X-API-Key` header). Requires the one-time asset download in setup step 6.
- `/openapi.json` — the OpenAPI schema (structure only, no inventory data)

---

## Project Structure

```
Inventory/
├── src/
│   ├── api/
│   │   ├── main.py             # FastAPI app, lifespan (migrations, recovery, poll loop), docs, static mount
│   │   ├── dependencies.py     # get_db(), verify_api_key(), verify_docs_access(), get_retailer_id()
│   │   ├── errors.py           # Shared DB-locked → 503 helper
│   │   ├── urls.py             # OpenFoodFacts / Sainsbury's hyperlink builders
│   │   ├── models.py           # Pydantic request/response models
│   │   ├── printer.py          # ESC/POS receipt-printer client (TM-T88IV over TCP)
│   │   ├── reports.py          # Report assembly (inventory / low-stock / unresolved)
│   │   └── routers/
│   │       ├── scan.py         # POST /scan
│   │       ├── session.py      # POST/GET /session, /session/confirm, /session/discard, PUT /session/items/{barcode}
│   │       ├── products.py     # GET /products/minimum-quantities, PUT /products/{barcode}/minimum_quantity
│   │       ├── printing.py     # POST /print/inventory, /print/low-stock
│   │       ├── reports.py      # GET /reports/inventory, /reports/low-stock, /reports/unresolved
│   │       └── ws.py           # Live-feed WebSocket (/ws)
│   ├── db/
│   │   ├── db.py               # get_connection(); WAL + per-connection pragmas
│   │   ├── initial_schema.sql  # Frozen baseline schema + seed data (run by initial migration)
│   │   ├── current_schema.sql  # Documentation: schema after all migrations
│   │   └── migrations/         # Alembic env + 5 migration scripts
│   ├── listener/
│   │   ├── main.py             # Run loop, reconnect, POSTs scans
│   │   ├── scanner.py          # evdev device enumeration + barcode reconstruction
│   │   └── validation.py       # is_valid_barcode()
│   └── worker/
│       ├── main.py             # Poll loop, two-phase resolution, OFF rate limiting
│       ├── off.py              # OpenFoodFacts client
│       └── sainsburys.py       # Sainsbury's price lookup
├── frontend/                   # One .html / .js / .css per page:
│   │                           #   feed (home), inventory, low_stock, min_quantities, unresolved
│   ├── base.css                # Global styles
│   ├── docs-nav.js / .css      # API-docs link + login modal (calls /docs-login)
│   ├── shared-utils.js         # esc() + sanitiseHref() (XSS-safe links)
│   ├── config.example.js       # API key template (copy to config.js)
│   ├── manifest.json           # Web app manifest (home-screen name, icons, theme)
│   ├── icons/                  # App / favicon icons (192/384/512/1024 px)
│   └── swagger-ui/             # Swagger UI assets (downloaded; gitignored)
├── systemd/
│   ├── fastapi.service
│   ├── listener.service
│   ├── worker.service
│   └── 99-inventory-scanner.rules
├── scripts/
│   ├── clear_products.sh            # Dev reset: clears cached product data + zero-qty inventory rows (Pi-only; uses systemctl)
│   ├── download_swagger_assets.sh   # Fetch pinned Swagger UI assets (run once for /docs)
│   ├── scan-sim.py                  # Dev barcode simulator → POST /scan
│   └── wipe_db.sh                   # Stop services, delete DB, restart (dev only)
├── tests/                      # 244 tests across api/, db/, listener/, worker/ (15 modules)
├── config.local.env            # Not committed — OFF_CONTACT_EMAIL, INVENTORY_API_KEY, PRINTER_IP
├── requirements.txt
├── alembic.ini
└── CHANGELOG.md
```

---

## Running Tests

```bash
pytest
```

244 tests across all modules.

---

## Useful Commands

```bash
# Service status
sudo systemctl status fastapi worker listener

# Follow logs
journalctl -u fastapi -f
journalctl -u listener -f
journalctl -u worker -f

# Inspect database
sqlite3 ~/repos/Inventory/inventory.db "SELECT * FROM inventory;"
sqlite3 ~/repos/Inventory/inventory.db "SELECT * FROM sessions;"
sqlite3 ~/repos/Inventory/inventory.db "SELECT * FROM session_items;"
sqlite3 ~/repos/Inventory/inventory.db "SELECT * FROM worker_state;"

# Simulate scans without the hardware (dev)
python scripts/scan-sim.py

# Reset database (development only)
~/repos/Inventory/scripts/wipe_db.sh
```

---

## Notes

- **Scanning requires an active session.** A `POST /scan` with no open session
  returns `409 no_active_session` rather than touching inventory
- A session's counts are applied to inventory only on **confirm**, signed by the
  session direction (added for `in`, subtracted for `out`); **discard** drops them
  with no change. Confirm is blocked while any of the session's lookups are still
  pending, and (for `out` sessions) if it would take an item below zero
- An interrupted session is recovered on API startup and left active to finish, so
  a restart mid-stock-take does not lose scanned items
- `StaticFiles` is mounted last in `main.py`, after all `include_router()` calls —
  it intercepts any path not matched by a registered route, so it must stay last
- `PRAGMA foreign_keys = ON`, `journal_mode = WAL`, `busy_timeout = 250`, and
  `synchronous = NORMAL` are set per-connection in `db.py` on every connection;
  they are not persistent database properties
- The scanner is identified by USB vendor ID `0x05e0` / product ID `0x1200`, not by
  device path, which is not stable across reboots
- Valid barcodes are digits only, 8–14 characters; anything else (including 2D
  codes like QR / DataMatrix) is rejected
- `INVENTORY_API_URL` overrides the URL the listener posts scans to (default
  `http://127.0.0.1:8000/scan`); `INVENTORY_DB` overrides the SQLite path (default
  `inventory.db` in the repo root). Neither is set by the shipped systemd units —
  add an `Environment=` line to the relevant unit if you need to change them
- The receipt printer (if used) is an EPSON TM-T88IV addressed over TCP port 9100
  at `PRINTER_IP`; printing is optional and independent of the rest of the system
- The live-feed WebSocket (`/ws`) is unauthenticated and broadcasts scan and
  resolution events to any client on the LAN that connects
- OpenFoodFacts is rate-limited client-side: the worker enforces a minimum
  4-second gap between calls and anchors the timing in the `worker_state` table so
  it survives restarts (the published "15 requests/min" is just the inverse of
  that gap; the server returns no rate-limit headers)
- The `scan_events` and `config` tables are legacy v1 carry-overs — present in the
  schema but unused by v2 code

---

## Upgrading from v2.0.x

This is a backwards-compatible upgrade — no API contract or external behaviour changed. Run the database migrations to swap the stored product-quantity representation (the `weight_g` column is replaced by a `product_quantity` text column, with existing values backfilled):

```bash
alembic upgrade head
```

The API also runs migrations automatically on startup, so restarting the service has the same effect.

---

## Upgrading from v1.x

**v2.0.0 is a breaking change.** The scanning model changed from a persistent
global *mode* to explicit *sessions*, and several v1 surfaces were removed:

- The `GET /mode` and `POST /mode` endpoints are gone — start a session instead
- Scanning no longer mutates inventory immediately; scans accumulate in a session
  and are applied on confirm. A scan with no active session now returns `409`
- The `pending_lookups` and `canonical_products` tables were dropped (handled by
  migration; run `alembic upgrade head`)
- The old `frontend/index.*` mode-toggle pages were removed; the home page is now
  the live feed
- Negative `delta` / `minimum_quantity` values are now rejected with `422`
  (previously `400`)

See [CHANGELOG.md](CHANGELOG.md) for the full list.
