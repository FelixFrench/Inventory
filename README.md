# Inventory v1.0.0

A barcode-driven grocery inventory system running on a Raspberry Pi 4B. Scan items in and out with a USB barcode scanner; product names, brands, and prices are resolved automatically in the background.

---

## Overview

- Scan groceries in or out using a Symbol LS2208 USB barcode scanner kept in the kitchen
- Scans are recorded immediately — background lookups never block scanning
- New barcodes are looked up automatically via OpenFoodFacts, then priced via the Sainsbury's internal API
- Inventory and low-stock reports are accessible from any device on the LAN via a mobile-first web UI
- All data is stored locally on the Pi in SQLite

---

## Architecture

Three systemd services run on the Pi:

| Service | Role |
|---------|------|
| `fastapi` | REST API (scan events, mode, reports) + serves the frontend |
| `listener` | Reads the USB scanner, POSTs barcodes to FastAPI |
| `worker` | Polls for unresolved barcodes, queries OpenFoodFacts and Sainsbury's |

**Data flow on a scan:**

1. LS2208 scans a barcode → `listener` detects keystrokes via `/dev/input/`
2. `listener` POSTs `{ "barcode": "..." }` to `http://127.0.0.1:8000/scan`
3. FastAPI reads the current mode (`in`/`out`) from the database and increments or decrements inventory immediately
4. If the barcode is new, a `pending_lookups` row is inserted; the scan is not blocked
5. `worker` picks up the pending row, queries OpenFoodFacts for product info, then Sainsbury's for price
6. Once resolved, all subsequent scans of that barcode update inventory directly

---

## Requirements

- Raspberry Pi 4B (or similar Linux host)
- Symbol LS2208 barcode scanner (USB HID)
- Python 3.11+

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

### 2. Configure environment

Create `config.local.env` (already in `.gitignore`):

```
OFF_CONTACT_EMAIL=your@email.com
INVENTORY_API_KEY=choose-a-strong-random-secret
```

`OFF_CONTACT_EMAIL` is used to construct the OpenFoodFacts `User-Agent` header (`FFInventory/1.0 (your@email.com)`), as required by the OFF API terms.

`INVENTORY_API_KEY` is the shared secret that protects all API endpoints. All three frontend pages and the listener service must present this key in the `X-API-Key` request header.

### 2a. Configure the frontend API key

Copy the example config and fill in the same key value:

```bash
cp frontend/config.example.js frontend/config.js
# then edit frontend/config.js and set API_KEY to match INVENTORY_API_KEY
```

`frontend/config.js` is gitignored and must be created manually on each installation.

### 3. Initialise the database

```bash
alembic upgrade head
```

This creates `inventory.db` with WAL mode enabled and seeds the `retailers` and `config` tables.

### 4. Scanner permissions (udev rule)

```bash
sudo cp systemd/inventory-scanner.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo usermod -aG input felix   # replace felix with your username
```

This grants the `input` group read access to the scanner device so the listener can run without root.

### 5. Install and start systemd services

```bash
# Edit <service-user> placeholder in each unit file to your username first
sudo cp systemd/fastapi.service /etc/systemd/system/
sudo cp systemd/listener.service /etc/systemd/system/
sudo cp systemd/worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fastapi listener worker
```

The web UI is then available at `http://<pi-ip>:8000` from any device on the LAN.

---

## Usage

### Mode switching

Switch between scan-in and scan-out from the web UI home page, or via the API:

```bash
curl -X POST http://localhost:8000/mode \
     -H "Content-Type: application/json" \
     -H "X-API-Key: <your-key>" \
     -d '{"mode":"in"}'
```

Mode persists across service restarts. Default is `out`.

### Reports

Navigate to `/inventory.html` (full inventory with quantities and prices) or `/low_stock.html` (items below their minimum quantity) in the browser, or call the API directly:

```
GET /reports/inventory
GET /reports/low-stock
```

Items with unresolved prices display as `—`.

---

## Project Structure

```
Inventory/
├── src/
│   ├── api/
│   │   ├── main.py             # FastAPI app, lifespan, router wiring, StaticFiles
│   │   ├── reports.py          # Report assembly (decoupled from rendering)
│   │   ├── dependencies.py     # get_db(), verify_api_key() dependencies
│   │   ├── models.py           # Pydantic request/response models
│   │   └── routers/
│   │       ├── scan.py         # POST /scan
│   │       ├── mode.py         # GET/POST /mode, WebSocket stub
│   │       └── reports.py      # GET /reports/inventory, GET /reports/low-stock
│   ├── db/
│   │   ├── db.py               # get_connection(); WAL mode; FK enforcement
│   │   ├── schema.sql          # Authoritative schema definition
│   │   └── migrations/         # Alembic migration scripts
│   ├── listener/
│   │   ├── main.py             # Run loop, reconnect logic, HTTP posting
│   │   ├── scanner.py          # Device enumeration and barcode reconstruction
│   │   └── validation.py       # is_valid_barcode()
│   ├── worker/
│   │   ├── main.py             # Poll loop, process_row, startup sleep
│   │   ├── off.py              # OpenFoodFacts client
│   │   └── sainsburys.py       # Sainsbury's price lookup
│   └── tests/
│       └── test_sainsburys.py  # Sainsbury's scraper tests (collected from src/)
├── frontend/
│   ├── index.html              # Mode toggle
│   ├── index.js
│   ├── inventory.html          # Inventory report
│   ├── inventory.js
│   ├── low_stock.html          # Low stock report
│   ├── low_stock.js
│   ├── config.example.js       # API key template (copy to config.js and fill in key)
│   └── config.js               # Not committed — contains API_KEY
├── systemd/
│   ├── fastapi.service
│   ├── listener.service
│   ├── worker.service
│   └── 99-inventory-scanner.rules
├── tests/
│   ├── conftest.py
│   ├── api/
│   ├── listener/
│   └── worker/
├── scripts/
│   └── wipe_db.sh
├── phase5_prototype.py
├── config.local.env            # Not committed — contains OFF_CONTACT_EMAIL, INVENTORY_API_KEY
├── requirements.txt
└── alembic.ini
```

---

## Running Tests

```bash
# From repo root — must be run without a path argument to collect all test locations
pytest
```

110 tests across all modules. `src/tests/test_sainsburys.py` lives under `src/` and is only collected when running `pytest` from the repo root with no path argument.

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
sqlite3 ~/repos/Inventory/inventory.db "SELECT * FROM pending_lookups;"
sqlite3 ~/repos/Inventory/inventory.db "SELECT * FROM scan_events ORDER BY id DESC LIMIT 10;"

# Reset database (development only)
~/repos/Inventory/scripts/wipe_db.sh
```

---

## Notes

- `StaticFiles` must be mounted last in `main.py`, after all `include_router()` calls — it intercepts any path not matched by a registered route
- `PRAGMA foreign_keys = ON` is a per-connection setting; it is set in `db.py` on every connection and cannot be relied upon as a persistent database property
- The scanner is identified by vendor ID `0x05e0` / product ID `0x1200`, not by device path, which is not stable across reboots
- Valid barcodes are digits only, 8–14 characters; 2D codes (QR, DataMatrix) are silently discarded
- `INVENTORY_API_URL` overrides the URL the listener posts scans to (default: `http://127.0.0.1:8000/scan`). Set this in the listener's systemd unit `Environment=` line if the API runs on a different host or port
- OpenFoodFacts rate limits: 15 req/min for barcode lookups, 10 req/min for search. The worker enforces a 4-second inter-request sleep client-side — no rate-limit headers are returned by the server
