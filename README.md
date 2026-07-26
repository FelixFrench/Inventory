# Inventory

A barcode-driven grocery inventory system running on a Raspberry Pi 4B. Open a scanning session, scan items in or out with a USB barcode scanner, review the batch on a live feed, then confirm it to inventory. Product names, brands, and prices are resolved automatically in the background.

Version history is in [CHANGELOG.md](CHANGELOG.md); releases are tagged in git.

This project has been built largely with Claude Code, and has served as an exercise for the author to learn to plan, review, and manage AI-driven software development.

---

## Overview

- Scanning is **session-based**: start an *in* or *out* session, scan a batch of items, then confirm the whole batch to inventory (or discard it)
- A **live feed** shows scanned items in real time as they arrive, including background-lookup status
- Scanning with no session open is allowed: nothing is written to inventory, but the barcode becomes known and any open search page jumps to its product page
- New barcodes are resolved automatically via OpenFoodFacts, then priced via the Sainsbury's API — lookups never block scanning. Failed lookups are retried and succeeded prices refreshed in the background, and any product can be refreshed on demand
- **Product groups** let several product variants (and other groups) count toward one shared minimum, so equivalent products are tracked together
- Inventory, low-stock, and unresolved-barcode reports are available from any device on the LAN via a mobile-first web UI, and can be sent to a receipt printer. The low-stock report covers groups and individual products separately
- Per-product and per-group pages show detail, group membership, and minimum stock levels, all editable in place
- All data is stored locally on the Pi in SQLite

---

## Scope and security

This system is designed for use on a **trusted home LAN**. It serves over plain HTTP (no TLS), the live-feed WebSocket (`/ws`) is unauthenticated, and there is no request rate limiting. **Do not expose it directly to the public internet.** If you need remote access, put it behind a VPN or an authenticating reverse proxy with TLS.

---

## How it works

### The session lifecycle

The central concept is the **session** - a bit like a basket:

1. **Start** a session as `in` (receiving stock) or `out` (removing stock). Only one session can be active at a time.
2. **Scan** items. Each scan records an *unsigned* count against its barcode in the active session — nothing is written to inventory yet. Scanning with **no** active session is not an error: it returns `200`, writes nothing to inventory or to the session, records only that the barcode exists, and is broadcast on the live feed marked `in_session: false`.
3. **Review** on the live feed as items arrive, adjusting counts if needed (for example to undo a misscan).
4. **Confirm** to apply the session to inventory — counts are *added* for an `in` session and *subtracted* for an `out` session — or **discard** to throw the session away with no inventory change.

Confirm is held back until every item's background lookup has finished, and (for `out` sessions) it's refused if it would take any item below zero. If the API restarts mid-session, the in-progress session is recovered on startup and left active for you to finish — it is never auto-confirmed or discarded — and the UI shows a brief recovery notice.

### Product identity

A product is identified by the composite **(barcode, retailer)** — a *variant*. Sainsbury's is the
only retailer configured, but the composite runs all the way through: it keys `inventory`,
`session_items`, `product_variants` and `prices`, it is carried on the wire as the `retailer` field
alongside the barcode, and it appears in the paths of the variant-addressed endpoints
(`/products/{barcode}/{retailer_id}/...`, `/session/items/{barcode}/{retailer_id}`).

### Product groups

A **group** collects things that should share one minimum-stock level — for example own-brand and
branded baked beans. A group's members are product variants and/or other groups:

- Groups can nest, and a member can belong to several groups. A group's total counts each distinct
  member variant once, however many paths reach it.
- A group with a minimum of `0` is organisational only: it never appears in the low-stock report.
- Deleting a group removes only its membership edges — member variants, sub-groups, and any group it
  belonged to survive.
- Self-membership and any edge that would create a cycle are rejected.

Groups are managed on `/groups.html` (create, rename, set minimum, delete) and `/group.html` (one
group's detail and members); a variant's memberships are also editable from `/product.html`.

### Background lookups, refresh and retry

The `worker` is a single process running one poll loop, in strict priority order:

1. **Info** for a scanned item that has none — OpenFoodFacts.
2. **Price** for an item whose info resolved — Sainsbury's.
3. **A user-requested refresh**, queued from the product page's refresh button.
4. **Background retry and refresh**, only when nothing above is waiting: failed OpenFoodFacts and
   price lookups are retried at most about once a day (giving up after five consecutive failures),
   and succeeded prices are re-checked about every 30 days so inflation doesn't go unnoticed. Per-kg
   prices keep their stored £/kg rather than being refreshed. Background work is limited to variants
   that are in stock or carry a minimum above zero.

Every lookup — success or failure — writes a durable row to the product/price cache with its status,
failure count, and timestamp, so a failure is a recorded fact rather than a missing row. The
OpenFoodFacts client-side gap is honoured before every call, whichever path makes it.

### Services

Three systemd services run on the Pi:

| Service | Role |
|---------|------|
| `fastapi` | REST API (sessions, scans, reports, printing, product and group detail and edits, minimum stock levels, refresh requests), the live-feed WebSocket, and serving the frontend |
| `listener` | Reads the USB scanner and POSTs barcodes to FastAPI |
| `worker` | The only process that calls OpenFoodFacts or Sainsbury's: resolves scanned barcodes, serves user-requested refreshes, and runs the background retry/refresh path |

### Data flow on a scan

1. The LS2208 scans a barcode → `listener` detects the keystrokes via `/dev/input/`
2. `listener` POSTs `{ "barcode": "..." }` to `http://127.0.0.1:8000/scan`
3. FastAPI appends the scan to the active session as an unsigned delta in `session_items` and pushes the update to any connected live-feed clients over the WebSocket. With no session open it returns `200` having written nothing but the `barcodes` row, and broadcasts a lean update marked `in_session: false`
4. If the barcode hasn't been resolved before, the `worker` picks it up from `session_items` and resolves it in two phases — OpenFoodFacts for product info, then Sainsbury's for price — without blocking scanning. Each phase writes a durable cache row whether it succeeds or fails
5. On **confirm**, the session's deltas are applied to inventory in explicit, row-count-checked transactions, and the session is cleared; on **discard**, the session is dropped with no inventory change

---

## Requirements

- Raspberry Pi 4B (or similar Linux host)
- Symbol LS2208 barcode scanner (USB HID; matched by USB vendor `0x05e0` / product `0x1200`)
- Python 3.11+
- The `python-escpos` library, which is a hard requirement (it is imported unconditionally by the print module) and is installed by `requirements.txt`
- *Optional:* an EPSON TM-T88IV network thermal printer (ESC/POS over TCP port 9100), for printing reports. Only the **hardware** is optional — leave `PRINTER_IP` unset and the print endpoints simply report that the printer is not configured

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

`OFF_CONTACT_EMAIL` is used to construct the OpenFoodFacts `User-Agent` header, as required by the OFF API terms; the worker won't start without it.

`INVENTORY_API_KEY` is the shared secret that protects the API. The frontend pages and the listener must present this key in the `X-API-Key` request header; the API won't start without it.

`PRINTER_IP` is optional. Leave it commented out unless you have the printer — the rest of the system runs normally without it, and the print endpoints simply return a "printer not configured" error.

### 2a. Configure the frontend API key

```bash
cp frontend/config.example.js frontend/config.js
# then edit frontend/config.js and set API_KEY to match INVENTORY_API_KEY
```

`frontend/config.js` is gitignored and must be created manually on each installation.

### 3. Initialise the database

```bash
alembic upgrade head
```

This creates `inventory.db` with WAL mode enabled, applies all migrations, and seeds the `retailers` table (Sainsbury's) and the `worker_state` rate-limit anchor. The API also runs migrations automatically on startup, so this step is optional if you start the service first — but it's useful to run by hand on a fresh install.

### 4. Scanner permissions (udev rule)

```bash
sudo cp systemd/99-inventory-scanner.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo usermod -aG input <user>   # replace <user> with your Linux username
```

This grants the `input` group read access to the scanner device so the listener can run without root.

### 5. Install and start systemd services

```bash
# The units run as user `<user>` from /path/to/repo — edit both to
# match your username and clone path first.
sudo cp systemd/fastapi.service /etc/systemd/system/
sudo cp systemd/listener.service /etc/systemd/system/
sudo cp systemd/worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fastapi listener worker
```

The web UI is then available at `http://<pi-ip>:8000` from any device on the LAN.

### 6. (Optional) Enable the API docs UI

The vendored Swagger UI bundle is not committed (only the small `swagger-init.js` that configures it
is). If you want the interactive docs at `/docs`, fetch the assets once:

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
3. Adjust a count if needed, then **confirm** to commit the batch to inventory, or **discard** to cancel it.

The same actions are available via the API. The session direction field is named `type`:

```bash
# Start an "in" session
curl -X POST http://localhost:8000/session \
     -H "Content-Type: application/json" \
     -H "X-API-Key: <your-key>" \
     -d '{"type":"in"}'
```

Then `POST /session/confirm` or `POST /session/discard` (no body), and
`PUT /session/items/{barcode}/{retailer_id}` with `{"delta": <n>}` to override a count. For exact
request/response shapes across all endpoints, use the interactive docs (below) rather than
hard-coding from here.

### Pages

Every page carries the same nav: Inventory, Low Stock, Feed, Unresolved, Groups, Search.

- `/feed.html` — live scan feed and session control (home page)
- `/inventory.html` — full inventory with quantities and prices
- `/low_stock.html` — two sections: groups below their minimum, then individual products below theirs
- `/unresolved.html` — barcodes whose product data is incomplete, with per-field resolution status
- `/groups.html` — list and manage groups (create, rename, set minimum, delete)
- `/group.html?id=<group_id>` — one group: its total, minimum, member variants and sub-groups
- `/product.html?barcode=<barcode>[&retailer_id=<id>]` — one variant: name, brand, quantity, price,
  minimum (editable), group membership (editable), a refresh button, and — while a session is open —
  a session banner with `[+]` / `[−]` controls
- `/search.html` — type a barcode to open its product page; a scan from anywhere on the LAN opens it
  too

Product names in the inventory, low-stock, feed and group listings link to that variant's product
page. Prices link out to the Sainsbury's product page where one is known, and the OpenFoodFacts
link (view, or add/edit when the lookup failed) sits on the unresolved report and the product page.
Items with unresolved prices display as `—`.

### Reports via the API

```
GET /reports/inventory
GET /reports/low-stock      # {"groups": [...], "products": [...]}
GET /reports/unresolved
```

### Print a report

With a printer configured (`PRINTER_IP` set), send a report to the receipt printer:

```
POST /print/inventory
POST /print/low-stock       # groups section, then products section
```

### Minimum stock levels

A minimum is set per variant, or per group. Editable on `/product.html` and `/group.html`
respectively, or via the API:

```
PUT /products/{barcode}/{retailer_id}/minimum_quantity   # body: {"minimum_quantity": <n>}
PATCH /groups/{group_id}                                 # body: {"minimum_quantity": <n>}
```

There is no all-minimums `GET`: read a variant's minimum from `GET /products/{barcode}/{retailer_id}`
and a group's from `GET /groups` or `GET /groups/{group_id}`.

### Products and groups

```
GET    /products/{barcode}/{retailer_id}                    # detail incl. group membership
POST   /products/{barcode}/{retailer_id}/refresh            # 202; queues a worker refresh
POST   /products/{barcode}/{retailer_id}/groups             # body: {"group_id": <n>}
DELETE /products/{barcode}/{retailer_id}/groups/{group_id}

GET    /groups
POST   /groups                                              # body: {"name": ..., "minimum_quantity": <n>}
GET    /groups/{group_id}
PATCH  /groups/{group_id}                                   # rename and/or set the minimum
DELETE /groups/{group_id}
POST   /groups/{group_id}/variants                          # body: {"barcode": ...}
DELETE /groups/{group_id}/variants/{barcode}
POST   /groups/{group_id}/subgroups                         # body: {"child_group_id": <n>}
DELETE /groups/{group_id}/subgroups/{child_group_id}
```

Group names are capped at 64 characters; a longer name is rejected with `422`. A refresh request
never calls OpenFoodFacts itself — it marks the variant and returns, and the worker does the work.

### Live feed WebSocket

`/ws` broadcasts four message types: `scan` (a barcode was scanned — `in_session: false` when no
session is open), `resolution` (a background lookup finished), `delta_update` (an item's session
count was overridden), and `refresh` (a durable lookup was refreshed outside a session). Payloads
carry `barcode` and `retailer` together, and the packaging size is the `quantity` field.

### API documentation

The app serves its own API docs:

- `/docs` — Swagger UI (gated; authenticate via `POST /docs-login`, or present the `X-API-Key` header). Requires the one-time asset download in setup step 6.
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
│   │   ├── groups.py           # Group resolution: WITH RECURSIVE walk, distinct-variant totals
│   │   └── routers/
│   │       ├── scan.py         # POST /scan
│   │       ├── session.py      # POST/GET /session, /session/confirm, /session/discard, PUT /session/items/{barcode}/{retailer_id}
│   │       ├── products.py     # Variant-addressed: PUT .../minimum_quantity, POST .../refresh, GET product detail, group membership
│   │       ├── groups.py       # Group CRUD, membership edges (the shared edge logic), GET /groups, GET /groups/{group_id}
│   │       ├── printing.py     # POST /print/inventory, /print/low-stock
│   │       ├── reports.py      # GET /reports/inventory, /reports/low-stock, /reports/unresolved
│   │       └── ws.py           # Live-feed WebSocket (/ws) + payload builders
│   ├── db/
│   │   ├── db.py               # get_connection(); WAL + per-connection pragmas
│   │   ├── initial_schema.sql  # Frozen baseline schema + seed data (run by initial migration)
│   │   ├── current_schema.sql  # Documentation only: schema after all migrations (regenerated, see Useful Commands)
│   │   └── migrations/         # Alembic env + the migration scripts
│   ├── listener/
│   │   ├── main.py             # Run loop, reconnect, POSTs scans
│   │   ├── scanner.py          # evdev device enumeration + barcode reconstruction
│   │   └── validation.py       # is_valid_barcode()
│   ├── worker/
│   │   ├── main.py             # Poll loop (info, price, manual refresh, background retry/refresh), OFF pacing
│   │   ├── off.py              # OpenFoodFacts client
│   │   └── sainsburys.py       # Sainsbury's price lookup
│   └── version.py              # __version__ — the single version constant
├── frontend/                   # One .html / .js / .css per page:
│   │                           #   feed (home), inventory, low_stock, unresolved,
│   │                           #   groups, group, product, search
│   ├── base.css                # Global styles
│   ├── docs-nav.js / .css      # API-docs link + login modal (calls /docs-login)
│   ├── shared-utils.js         # esc(), sanitiseHref() (XSS-safe links), showToast(), rowKey()
│   ├── *.test.js               # Frontend unit tests (node --test)
│   ├── config.example.js       # API key template (copy to config.js)
│   ├── manifest.json           # Web app manifest (home-screen name, icons, theme)
│   ├── icons/                  # App / favicon icons (192/384/512/1024 px)
│   └── swagger-ui/             # swagger-init.js (committed) + the downloaded UI bundle (gitignored)
├── systemd/
│   ├── fastapi.service
│   ├── listener.service
│   ├── worker.service
│   └── 99-inventory-scanner.rules
├── scripts/
│   ├── clear_products.sh            # Dev reset: clears cached product data + zero-qty inventory rows (Pi-only; uses systemctl)
│   ├── download_swagger_assets.sh   # Fetch pinned Swagger UI assets (run once for /docs)
│   ├── regen_current_schema.py      # Regenerate / verify src/db/current_schema.sql
│   ├── scan-sim.py                  # Dev barcode simulator → POST /scan
│   └── wipe_db.sh                   # Stop services, delete DB, restart (dev only)
├── tests/                      # Backend tests across api/, db/, listener/, worker/, plus test_version.py
├── config.local.env            # Not committed — OFF_CONTACT_EMAIL, INVENTORY_API_KEY, PRINTER_IP
├── requirements.txt
├── alembic.ini
├── mypy.ini
└── CHANGELOG.md
```

---

## Running Tests

The backend suite (`api/`, `db/`, `listener/`, `worker/`, and the version check):

```bash
pytest
```

The frontend unit tests, which need no browser and no server:

```bash
cd frontend && node --test
```

Linting and type checking:

```bash
ruff check src/ tests/ scripts/
mypy src/
```

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

# Print the live database's schema. Close to src/db/current_schema.sql, but not identical:
# the sqlite3 CLI adds "IF NOT EXISTS" to tables a migration rebuilt, which is CLI
# presentation rather than stored DDL.
sqlite3 inventory.db ".schema"

# Regenerate src/db/current_schema.sql (documentation only — never edits initial_schema.sql).
# Migrates a throwaway database and dumps the DDL sqlite itself stored, so the result is
# host-independent. --verify checks the committed file object by object and writes nothing.
python scripts/regen_current_schema.py --write
python scripts/regen_current_schema.py --verify
```

---

## Notes

- **Scanning without a session is allowed and writes nothing.** A `POST /scan` with no open session returns `200`, leaves `inventory` and `session_items` untouched, records only that the barcode exists (so a minimum or group membership can be set on it later), and broadcasts a lean live-feed message marked `in_session: false`
- A session's counts are applied to inventory only on **confirm**, signed by the session direction (added for `in`, subtracted for `out`); **discard** drops them with no change. Confirm is blocked while any of the session's lookups are still pending, and (for `out` sessions) if it would take an item below zero
- An interrupted session is recovered on API startup and left active to finish, so a restart mid-stock-take does not lose scanned items
- `StaticFiles` is mounted last in `main.py`, after all `include_router()` calls — it intercepts any path not matched by a registered route, so it must stay last
- `PRAGMA foreign_keys = ON`, `journal_mode = WAL`, `busy_timeout = 250`, and `synchronous = NORMAL` are set per-connection in `db.py` on every connection; they are not persistent database properties
- The scanner is identified by USB vendor ID `0x05e0` / product ID `0x1200`, not by device path, which is not stable across reboots
- Valid barcodes are digits only, 8–14 characters; anything else (including 2D codes like QR / DataMatrix) is rejected
- `INVENTORY_API_URL` overrides the URL the listener posts scans to (default `http://127.0.0.1:8000/scan`); `INVENTORY_DB` overrides the SQLite path (default `inventory.db` in the repo root). Neither is set by the shipped systemd units — add an `Environment=` line to the relevant unit if you need to change them
- The receipt printer (if used) is an EPSON TM-T88IV addressed over TCP port 9100 at `PRINTER_IP`. The *hardware* is optional and independent of the rest of the system; the `python-escpos` *library* is not — it is imported unconditionally, so it must be installed either way
- Text that reaches the printer (product and group names) is truncated and has control characters stripped at the output boundary, because an ESC/POS stream treats control bytes as commands
- The live-feed WebSocket (`/ws`) is unauthenticated and broadcasts `scan`, `resolution`, `delta_update`, and `refresh` messages to any client on the LAN that connects
- OpenFoodFacts is rate-limited client-side: the worker enforces a minimum 4-second gap before *every* call, wherever in the poll loop it originates, and anchors the timing in the `worker_state` table so the gap survives restarts (the published "15 requests/min" is just the inverse of that gap; the server returns no rate-limit headers)
- Only the `worker` process ever calls OpenFoodFacts or Sainsbury's. FastAPI's refresh endpoint just marks a row for the worker, which keeps the single-caller invariant the rate limiting depends on
- The legacy v1 `scan_events` and `config` tables have been **dropped by migration** and are not referenced by any `api/`, `worker/` or `listener/` code. They still appear — correctly — in the frozen `initial_schema.sql`, in the migration that drops them, and in migration tests that reconstruct the older schema
- `src/db/initial_schema.sql` is frozen: it is the baseline the first migration runs, and is never edited. `src/db/current_schema.sql` is documentation only, regenerated from a fresh migration run (see Useful Commands); nothing reads it at runtime

---

## Upgrading to v3.0.0

**This is a breaking release.** The externally-visible contract changes below are what make it a
major version rather than a minor one; any client written against v2.x needs review.

1. **The wire field `weight` is now `quantity`.** Every REST and WebSocket payload that carried the
   packaging size renames the field. The visible column header becomes "Quantity" and the CSS class
   `col-weight` becomes `col-quantity`. The database column was already `product_quantity`, so this
   is an API-layer rename — but a client reading `weight` sees nothing.
2. **Payloads now carry `retailer`.** WebSocket and session payloads emit the retailer id alongside
   the barcode, so the `(barcode, retailer)` pair that identifies a variant is complete on the wire.
3. **`POST /scan` no longer returns `409 no_active_session`.** A sessionless scan returns `200`,
   writes nothing to inventory or the session, and is broadcast marked `in_session: false`. A client
   that treated `409` as "scan rejected" will now see a success it must interpret.
4. **`/min_quantities.html` and the all-minimums `GET` are gone.** Setting a minimum is
   variant-addressed: `PUT /products/{barcode}/{retailer_id}/minimum_quantity`. Minimums are edited
   on the product and group pages. The session override moved the same way, to
   `PUT /session/items/{barcode}/{retailer_id}`.
5. **`GET /reports/low-stock` returns two sections.** The response is now
   `{"groups": [...], "products": [...]}` rather than a single item list, and the printed receipt
   matches.
6. **Group names are capped at 64 characters**, with `422` for anything longer.

### Prerequisites

- `python-escpos` is required, not optional — it is imported unconditionally by the print module.
  `pip install -r requirements.txt` covers it. Only the printer hardware (`PRINTER_IP`) is optional.
- On the Pi, transitive dependencies were security-upgraded **in the virtualenv only** —
  `pillow>=12.3.0`, `msgpack>=1.2.1`, `setuptools>=83.0.0` — with no change to
  `requirements.txt`, following this project's practice for transitive upgrades:

  ```bash
  source ~/repos/Inventory/.venv/bin/activate
  pip install --upgrade 'pillow>=12.3.0' 'msgpack>=1.2.1' 'setuptools>=83.0.0'
  ```

### Steps

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
sudo systemctl restart fastapi worker listener
```

The migrations re-key `inventory` and `session_items` to `(barcode, retailer_id)`, move
`minimum_quantity` onto `product_variants`, add the product-group tables and the durable
lookup-state columns, and drop the legacy `scan_events` / `config` tables. The API also runs
migrations on startup, so restarting the service has the same effect as running them by hand.

### Cutting a release

`src/version.py` is the **single bump point**: the OpenFoodFacts `User-Agent` and the
OpenAPI/Swagger `version` both read `__version__` from it, and no version literal belongs anywhere
else in the code. Release order: bump `src/version.py` → add the CHANGELOG entry (a test asserts the
two agree) → regenerate `src/db/current_schema.sql` if migrations changed → commit → tag. Versions
live in the CHANGELOG and in git tags, not in prose documentation.

---

## Upgrading from v2.0.x

*Historical: the v2.0.x → v2.1.0 upgrade, kept for reference.*

This was a backwards-compatible upgrade — no API contract or external behaviour changed. Run the database migrations to swap the stored product-quantity representation (the `weight_g` column is replaced by a `product_quantity` text column, with existing values backfilled):

```bash
alembic upgrade head
```

The API also runs migrations automatically on startup, so restarting the service has the same effect.

---

## Upgrading from v1.x

*Historical: the v1.x → v2.0.0 upgrade, kept for reference.*

**v2.0.0 is a breaking change.** The scanning model changed from a persistent global *mode* to explicit *sessions*, and several v1 surfaces were removed:

- The `GET /mode` and `POST /mode` endpoints are gone — start a session instead
- Scanning no longer mutates inventory immediately; scans accumulate in a session and are applied on confirm. In v2.x a scan with no active session returned `409` — that rejection was itself removed in v3.0.0, which returns `200` and writes nothing (see [Upgrading to v3.0.0](#upgrading-to-v300))
- The `pending_lookups` and `canonical_products` tables were dropped (handled by migration; run `alembic upgrade head`)
- The old `frontend/index.*` mode-toggle pages were removed; the home page is now the live feed
- Negative `delta` / `minimum_quantity` values are now rejected with `422` (previously `400`)

See [CHANGELOG.md](CHANGELOG.md) for the full list.

---

## Price data and the Sainsbury's lookup

The optional Sainsbury's price lookup (`src/worker/sainsburys.py`) queries an undocumented internal Sainsbury's endpoint — not a public API — sending a browser-like `User-Agent`. It is included for personal, educational reference only. This project is not affiliated with or endorsed by Sainsbury's; the product and price data belong to Sainsbury's, and automated access may be contrary to their website terms. Keep any use low-volume and personal — it is not intended for bulk or commercial data collection — and you are responsible for ensuring your use complies with Sainsbury's terms and applicable law.

OpenFoodFacts, by contrast, is a public API used within its stated terms: the `OFF_CONTACT_EMAIL` you configure is sent in the `User-Agent` as the API requires, and requests are rate-limited client-side.

---

## Licence

This project is free software, licensed under the GNU General Public License v3.0 (GPLv3) — see the [LICENSE](LICENSE) file for the full text. GPLv3 governs the software in this repository (and permits commercial use of the *code*); it does not grant any rights in third-party services the software interacts with — see [Price data and the Sainsbury's lookup](#price-data-and-the-sainsburys-lookup) above.