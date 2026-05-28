# Phase 2 Dev Report — WebSocket Live Scan Feed

## 1. Codebase findings (Step 0)

### `src/api/main.py`
- Lifespan had no background tasks; `yield` was the last statement with no shutdown code.
- StaticFiles was correctly last (line 73).
- `app.state.sainsburys_retailer_id` was already set at startup from a `SELECT id FROM retailers WHERE name = 'Sainsbury''s'` query — constraint #3 was already handled.
- CSP header already included `connect-src 'self' ws:` so WebSocket connections were already permitted by the existing middleware.
- All three existing routers used `dependencies=[Depends(verify_api_key)]`.

### `src/api/routers/scan.py`
- **Sync `def`** — converted to `async def` in Phase 2.
- Used `Depends(get_retailer_id)` as a FastAPI dependency. Phase 2 changes to `request.app.state.sainsburys_retailer_id` directly via a `Request` parameter.
- The existing check used two `EXISTS(...)` subqueries. These were merged into a single LEFT JOIN that also fetches name/brand/weight_g/price_pence in one round-trip, satisfying the spec's "no additional DB round-trip" requirement.

### `src/api/routers/session.py`
- Uses `BEGIN IMMEDIATE` + manual commit/rollback pattern. Not modified in Phase 2.

### `src/db/db.py`
- `get_connection()` sets `check_same_thread=False`, WAL, `busy_timeout=250`, `row_factory=sqlite3.Row`. The `row_factory` means `sqlite3.Row` objects support dict-style key access, which is compatible with `build_payload`'s `row['key']` interface.

### `frontend/inventory.html`
- Nav is entirely static HTML (no JS management). Only the HTML needed updating.

### `tests/conftest.py`
- Minimal — only mocks `evdev`. All fixtures and `_NocloseConn` are per-test-file.
- `TestClient` is used without a `with` block in all existing fixtures, so the FastAPI lifespan does **not** run during tests. This means the poll loop background task never starts in tests, and `app.state.sainsburys_retailer_id` is set manually in each test fixture.

---

## 2. Deviations from spec

### 2a. `Depends(get_retailer_id)` removed from scan.py
The spec's Round 2 feedback noted that `get_retailer_id` should be replaced with direct `request.app.state` access. `get_retailer_id` was in fact defined in `src/api/dependencies.py` (it already read from `app.state`), but per the approved plan, `scan.py` now uses `Request` directly.

### 2b. Scan check query restructured
The original `EXISTS(...)` check was replaced with a LEFT JOIN that also fetches the field values, eliminating an otherwise-needed second round-trip to populate the scan payload. The `has_info` and `has_price` flags are derived from `pv.barcode IS NOT NULL` and `pr.barcode IS NOT NULL` respectively.

### 2c. Test baseline mismatch
The spec states the Phase 1 baseline was 127 tests. The actual baseline on this branch was 95 tests. The Phase 2 additions bring the total to 111 (95 + 16 new). All tests pass. The 127 figure appears to be from an earlier state of the codebase not reflected in the current branch.

### 2d. `_compute_poll_updates` placement
Per the approved plan, `_compute_poll_updates` and `_poll_tick` are in `main.py` (not `ws.py`) so they can be directly imported and called synchronously from `test_ws.py` without pytest-asyncio.

---

## 3. Async/broadcast decision

### scan.py async conversion
`scan.py` was converted from `def` to `async def`. The DB work was extracted into `_do_scan(barcode, retailer_id) -> dict`, which is called via `await asyncio.to_thread(_do_scan, ...)`. The helper also fetches name/brand/weight_g/price_pence in the same transaction so the broadcast payload can be built immediately after commit.

After `asyncio.to_thread` returns the result dict, the route calls:
```python
await manager.broadcast(json.dumps(build_payload("scan", result)))
```

The broadcast is only reached on the success path. HTTPException raised inside `_do_scan` (e.g., 409 no_active_session) propagates through `asyncio.to_thread` before broadcast is reached.

### Test fixture changes
- `test_scan_phase1.py`: removed `app.dependency_overrides[get_retailer_id]`, replaced with `app.state.sainsburys_retailer_id = _RETAILER_ID`. The existing `patch("src.api.routers.scan.get_connection", ...)` patch continues to work — `asyncio.to_thread` calls `_do_scan` in a thread, which calls the module-level `get_connection` that has been patched.
- `test_scan.py`: already used `app.state.sainsburys_retailer_id = 1`, no changes needed.

---

## 4. Test count

| | Count |
|---|---|
| Previous (actual baseline on branch) | 95 |
| New tests added (`test_ws.py`) | 16 |
| **New total** | **111** |

New tests cover: ConnectionManager (4), scan broadcast (2), poll loop logic (5), status mapping (4), WS endpoint end-to-end (1).

---

## 5. Open questions and deployment risks

### 5a. Poll loop lifespan (not tested)
The `_session_poll_loop` background task is started in the lifespan and cancelled on shutdown. This is exercised only in the live server, not in tests (TestClient doesn't run the lifespan in non-context-manager usage). Verify on deploy that:
- The loop starts cleanly (check logs for `Inventory FastAPI started`)
- Resolution broadcasts arrive in the browser within ~2 seconds of a worker updating `product_variants`
- The server shuts down cleanly without `CancelledError` leaking to logs

### 5b. WebSocket reconnect
The `onclose` handler schedules `setTimeout(connectWS, 2000)`. Verify by restarting the server while the feed page is open — the WS badge/status should recover within 3 seconds.

### 5c. Recovery toast timing
`session.recovered_at` is set in UTC. The client compares against `Date.now()`. If the server and client clocks differ by more than a few seconds, the 5-minute threshold may behave unexpectedly. Acceptable on a LAN deployment where clocks are typically in sync.

### 5d. `GET /` redirect
The redirect to `/feed.html` was added as a route before StaticFiles. Verify `curl http://<pi>:8000/ -v` returns `307 Temporary Redirect` to `/feed.html`, and that the browser follows through to the feed page.

### 5e. CSP `connect-src ws:`
The existing CSP header permits `ws:` (unencrypted WebSocket) but not `wss:`. This is correct for a local Pi deployment. If HTTPS/WSS is added in future, the header will need updating.

### 5f. Quantity controls
The `−` and `+` buttons in each feed row are rendered disabled. Phase 3 wires them to `PUT /session/items/{barcode}`. The TODO comment in `feed.js` marks the exact location.
