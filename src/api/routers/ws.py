# SPIKE A — REMOVE BEFORE PHASE 2
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        dead = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.active_connections.remove(connection)


manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            await manager.broadcast(data)
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# SPIKE A — REMOVE BEFORE PHASE 2
# SECURITY NOTE: Intentionally unauthenticated — no API key check.
# Triggers a server-side broadcast to ALL connected WebSocket clients.
# Acceptable only on this LAN-only spike. Must be deleted before Phase 2.
@router.get("/spike/broadcast")
async def spike_broadcast(msg: str = "hello"):
    await manager.broadcast(msg)
    return {"broadcast": msg, "clients": len(manager.active_connections)}
