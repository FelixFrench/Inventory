from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


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
        dead = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)


manager = ConnectionManager()


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


def _status_to_wire(db_status: str) -> str:
    if db_status == "pending":
        return "loading"
    if db_status == "resolved":
        return "resolved"
    return "failed"  # 'failed' and 'not_possible'


def _format_weight(weight_g) -> str | None:
    if weight_g is None:
        return None
    if weight_g >= 1000:
        return f"{weight_g / 1000:g}kg"
    return f"{weight_g:g}g"


def build_payload(type_: str, row) -> dict:
    """Build wire payload from a DB row or dict.

    Row must provide keys: barcode, session_delta, info_status, price_status,
    name, brand, weight_g, price_pence. Works with sqlite3.Row objects (which
    support dict-style access when row_factory = sqlite3.Row) and plain dicts.
    """
    info_wire = _status_to_wire(row["info_status"])
    price_wire = _status_to_wire(row["price_status"])

    if info_wire == "resolved":
        name_val = row["name"]
        brand_val = row["brand"]
        weight_val = _format_weight(row["weight_g"])
    else:
        name_val = brand_val = weight_val = None

    if price_wire == "resolved":
        price_val = row["price_pence"] / 100 if row["price_pence"] is not None else None
    else:
        price_val = None

    return {
        "type": type_,
        "barcode": row["barcode"],
        "name":   {"value": name_val,   "status": info_wire},
        "brand":  {"value": brand_val,  "status": info_wire},
        "weight": {"value": weight_val, "status": info_wire},
        "price":  {"value": price_val,  "status": price_wire},
        "session_delta": row["session_delta"],
    }
