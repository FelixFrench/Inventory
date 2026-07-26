from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.api.urls import off_url as build_off_url
from src.api.urls import product_page_url

router = APIRouter(tags=["WebSocket"])


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        if not self.active_connections:
            return
        dead = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)


manager = ConnectionManager()


# Resolution-broadcast poll query for the FastAPI background task
# (_session_poll_loop in main.py). Kept here because its column list is the
# contract consumed by build_payload below — the two must stay in sync.
POLL_QUERY = """
SELECT si.barcode,
       si.retailer_id,
       si.delta AS session_delta,
       si.info_status,
       si.price_status,
       pv.name,
       pv.brand,
       pv.product_quantity,
       pr.price_pence,
       pr.product_url
FROM   session_items si
LEFT   JOIN product_variants pv
           ON pv.barcode = si.barcode AND pv.retailer_id = ?
LEFT   JOIN prices pr
           ON pr.barcode = si.barcode AND pr.retailer_id = ?
JOIN   sessions s ON s.id = si.session_id
"""


# Refresh-broadcast poll query for the FastAPI background task (_session_poll_loop in main.py).
# A LEFT JOIN of the two durable cache tables on (barcode, retailer_id): product_variants is the OFF
# side (its lookup_status/last_lookup_datetime), prices the price side (aliased price_status /
# price_ts; NULL for every price column when no prices row exists). The column list is the contract
# consumed by build_refresh_notification and _compute_refresh_updates — keep them in sync.
REFRESH_POLL_QUERY = """
SELECT pv.barcode,
       pv.retailer_id,
       pv.lookup_status,
       pv.name,
       pv.brand,
       pv.product_quantity,
       pv.last_lookup_datetime AS off_ts,
       pr.price_pence,
       pr.price_type,
       pr.product_url,
       pr.lookup_status         AS price_status,
       pr.last_lookup_datetime  AS price_ts
FROM   product_variants pv
LEFT   JOIN prices pr
           ON pr.barcode = pv.barcode AND pr.retailer_id = pv.retailer_id
"""


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """
    Persistent WebSocket connection for real-time session updates.

    Pushes JSON messages of type 'scan' (new barcode scanned), 'resolution'
    (product info or price lookup completed), and 'delta_update' (item delta
    manually adjusted). The connection is kept open until the client disconnects.
    """
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


_STATUS_MAP = {"pending": "loading", "resolved": "resolved"}

def _status_to_wire(db_status: str) -> str:
    return _STATUS_MAP.get(db_status, "failed")  # 'failed' and 'not_possible' → 'failed'


def build_payload(type_: str, row, retailer_id: int) -> dict:
    """Build wire payload from a DB row or dict.

    Row must provide keys: barcode, session_delta, info_status, price_status,
    name, brand, product_quantity, price_pence, product_url. Works with sqlite3.Row
    objects (which support dict-style access when row_factory = sqlite3.Row)
    and plain dicts.

    ``retailer_id`` is emitted as the ``retailer`` field: the unambiguous durable key,
    always Sainsbury's this sprint. Provisional wire identity for 1b — 2c may map
    id -> slug without changing it. Frontend does not consume it yet (deferred to 2e).
    """
    info_wire = _status_to_wire(row["info_status"])
    price_wire = _status_to_wire(row["price_status"])

    if info_wire == "resolved":
        name_val = row["name"]
        brand_val = row["brand"]
        quantity_val = row["product_quantity"]
    else:
        name_val = brand_val = quantity_val = None

    if price_wire == "resolved":
        price_val = row["price_pence"] / 100 if row["price_pence"] is not None else None
    else:
        price_val = None

    return {
        "type": type_,
        "barcode": row["barcode"],
        "retailer": retailer_id,
        "name":     {"value": name_val,     "status": info_wire},
        "brand":    {"value": brand_val,    "status": info_wire},
        "quantity": {"value": quantity_val, "status": info_wire},
        "price":    {"value": price_val,    "status": price_wire},
        "session_delta": row["session_delta"],
        "off_url":   build_off_url(row["barcode"], info_status=row["info_status"]),
        "product_page_url": product_page_url(row["barcode"], retailer_id),
        "price_url": row["product_url"],
    }


def build_scan_notification(barcode: str, retailer_id: int) -> dict:
    """Lean 'scan' payload for a sessionless scan.

    Unlike ``build_payload`` this needs no resolved product row: a sessionless scan
    has none of ``name``/``brand``/``product_quantity``/``price_pence``/``product_url``/
    ``session_delta``/``info_status``/``price_status`` (build_payload would KeyError).
    ``retailer`` carries the retailer_id, mirroring build_payload's wire field name.
    """
    return {
        "type": "scan",
        "barcode": barcode,
        "retailer": retailer_id,
        "in_session": False,
    }


def build_refresh_notification(row) -> dict:
    """Lean 'refresh' payload for a session-less durable write (Sprint 2, Phase 3c).

    Analogous to build_scan_notification, but carries the refreshed OFF/price fields so an open
    product page can update in place. Fired by the FastAPI poll loop when a durable lookup timestamp
    advances (manual refresh, or the 3b background scheduler). ``row`` is a LEFT JOIN of
    product_variants (aliased ``lookup_status`` = the OFF side) and prices (aliased ``price_status``
    = prices.lookup_status, NULL when no prices row exists).

    Reuses build_payload's DB->wire status mapping (``_status_to_wire``: pending->loading,
    resolved->resolved, everything else incl. NULL/absent->failed) and its value-gating invariant: a
    field's value is carried only when its wire status is 'resolved', else null. So an OFF-failed
    refresh carries name/brand/quantity=null (the page keeps what it shows); a price-failed or
    not-attempted refresh carries price_pence/product_url=null. Unlike build_payload's 'price' field,
    price_pence is the raw integer pence (not /100), per the 3c wire shape.
    """
    off_wire = _status_to_wire(row["lookup_status"])
    price_wire = _status_to_wire(row["price_status"])

    if off_wire == "resolved":
        name_val = row["name"]
        brand_val = row["brand"]
        quantity_val = row["product_quantity"]
    else:
        name_val = brand_val = quantity_val = None

    if price_wire == "resolved":
        price_pence_val = row["price_pence"]
        price_type_val = row["price_type"]
        product_url_val = row["product_url"]
    else:
        price_pence_val = None
        product_url_val = None
        price_type_val = row["price_type"]  # carried through, benign (schema DEFAULT 'unit')

    return {
        "type": "refresh",
        "barcode": row["barcode"],
        "retailer": row["retailer_id"],
        "off_status": off_wire,
        "price_status": price_wire,
        "name": name_val,
        "brand": brand_val,
        "quantity": quantity_val,
        "price_pence": price_pence_val,
        "price_type": price_type_val,
        "product_url": product_url_val,
        "off_url": build_off_url(row["barcode"], name=name_val),
    }
