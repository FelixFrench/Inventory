import asyncio
import json
import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import get_retailer_id
from src.api.models import (
    ConfirmResponse,
    DeltaUpdateRequest,
    DiscardResponse,
    SessionObject,
    SessionResponse,
    StartSessionRequest,
)
from src.api.routers.ws import manager
from src.db.db import get_connection

router = APIRouter()

_503 = HTTPException(
    status_code=503,
    detail="Service temporarily unavailable",
    headers={"Retry-After": "1"},
)


def _build_session_object(conn: sqlite3.Connection, retailer_id: int, session_row) -> SessionObject:
    total_delta = conn.execute(
        "SELECT COALESCE(SUM(delta), 0) FROM session_items WHERE session_id = ?",
        (session_row['id'],)
    ).fetchone()[0]

    rows = conn.execute(
        """
        SELECT si.barcode,
               si.delta,
               si.info_status,
               si.price_status,
               si.first_scanned_at,
               pv.name,
               pv.brand,
               pv.weight_g,
               pr.price_pence,
               COALESCE(inv.quantity, 0) AS inventory_quantity
        FROM   session_items si
        LEFT   JOIN product_variants pv ON pv.barcode = si.barcode AND pv.retailer_id = ?
        LEFT   JOIN prices pr            ON pr.barcode = si.barcode AND pr.retailer_id = ?
        LEFT   JOIN inventory inv        ON inv.barcode = si.barcode
        WHERE  si.session_id = ?
        ORDER  BY si.first_scanned_at ASC
        """,
        (retailer_id, retailer_id, session_row['id'])
    ).fetchall()

    items = []
    for r in rows:
        info_s = "loading" if r['info_status'] == "pending" else r['info_status']
        price_s = "loading" if r['price_status'] == "pending" else (
            "failed" if r['price_status'] == "not_possible" else r['price_status']
        )

        weight_val = f"{r['weight_g']:g}g" if r['weight_g'] is not None else None
        price_val = r['price_pence'] / 100 if r['price_pence'] is not None else None

        items.append({
            "barcode": r['barcode'],
            "delta": r['delta'],
            "inventory_quantity": r['inventory_quantity'],
            "first_scanned_at": r['first_scanned_at'],
            "name": {"value": r['name'], "status": info_s},
            "brand": {"value": r['brand'], "status": info_s},
            "weight": {"value": weight_val, "status": info_s},
            "price": {"value": price_val, "status": price_s},
        })

    return SessionObject(
        id=session_row['id'],
        type=session_row['type'],
        started_at=session_row['started_at'],
        recovered_at=session_row['recovered_at'],
        total_delta=total_delta,
        items=items,
    )


@router.post("/session", status_code=201)
def start_session(body: StartSessionRequest, retailer_id: int = Depends(get_retailer_id)):
    try:
        conn = get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id, type, started_at, recovered_at FROM sessions LIMIT 1"
            ).fetchone()
            if existing:
                session_obj = _build_session_object(conn, retailer_id, existing)
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={"error": "session_already_active", "session": session_obj.model_dump()},
                )
            conn.execute(
                "INSERT INTO sessions (type, started_at) VALUES (?, ?)",
                (body.type, datetime.utcnow().isoformat())
            )
            row = conn.execute(
                "SELECT id, type, started_at, recovered_at FROM sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            session_obj = _build_session_object(conn, retailer_id, row)
            conn.commit()
        except:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
        return {"session": session_obj.model_dump()}
    except HTTPException:
        raise
    except sqlite3.OperationalError:
        raise _503


@router.get("/session", response_model=SessionResponse)
def get_session(retailer_id: int = Depends(get_retailer_id)):
    try:
        conn = get_connection()
        try:
            session_row = conn.execute(
                "SELECT id, type, started_at, recovered_at FROM sessions LIMIT 1"
            ).fetchone()
            if session_row is None:
                return SessionResponse(session=None)
            session_obj = _build_session_object(conn, retailer_id, session_row)
            return SessionResponse(session=session_obj)
        finally:
            conn.close()
    except sqlite3.OperationalError:
        raise _503


@router.post("/session/confirm", response_model=ConfirmResponse)
def confirm_session(retailer_id: int = Depends(get_retailer_id)):
    try:
        conn = get_connection()
        try:
            session_row = conn.execute(
                "SELECT id, type FROM sessions LIMIT 1"
            ).fetchone()
            if session_row is None:
                raise HTTPException(status_code=409, detail={"error": "no_active_session"})

            session_id = session_row['id']
            session_type = session_row['type']

            pending_count = conn.execute(
                "SELECT COUNT(*) FROM session_items "
                "WHERE session_id = ? AND (info_status = 'pending' OR price_status = 'pending')",
                (session_id,)
            ).fetchone()[0]
            if pending_count > 0:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "lookups_pending", "pending_count": pending_count}
                )

            conn.execute("BEGIN IMMEDIATE")
            try:
                if session_type == "out":
                    negative_rows = conn.execute(
                        """
                        SELECT si.barcode,
                               COALESCE(inv.quantity, 0) AS current_qty,
                               si.delta
                        FROM   session_items si
                        LEFT   JOIN inventory inv ON inv.barcode = si.barcode
                        WHERE  si.session_id = ?
                          AND  (COALESCE(inv.quantity, 0) - si.delta) < 0
                        """,
                        (session_id,)
                    ).fetchall()
                    if negative_rows:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "error": "would_go_negative",
                                "items": [
                                    {
                                        "barcode": r['barcode'],
                                        "current_quantity": r['current_qty'],
                                        "delta": r['delta'],
                                    }
                                    for r in negative_rows
                                ],
                            }
                        )

                if session_type == "in":
                    conn.execute(
                        """
                        INSERT INTO inventory (barcode, quantity)
                        SELECT barcode, delta FROM session_items WHERE session_id = ? AND delta != 0
                        ON CONFLICT(barcode) DO UPDATE SET quantity = quantity + excluded.quantity
                        """,
                        (session_id,)
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO inventory (barcode, quantity)
                        SELECT barcode, delta FROM session_items WHERE session_id = ? AND delta != 0
                        ON CONFLICT(barcode) DO UPDATE SET quantity = quantity - excluded.quantity
                        """,
                        (session_id,)
                    )

                applied = conn.execute(
                    "SELECT COUNT(*) FROM session_items WHERE session_id = ? AND delta != 0",
                    (session_id,)
                ).fetchone()[0]

                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                conn.commit()
            except:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

            return ConfirmResponse(applied_items=applied, session_id=session_id)
        finally:
            conn.close()
    except HTTPException:
        raise
    except sqlite3.OperationalError:
        raise _503


@router.post("/session/discard", response_model=DiscardResponse)
def discard_session():
    try:
        conn = get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            session_row = conn.execute("SELECT id FROM sessions LIMIT 1").fetchone()
            if session_row is None:
                conn.rollback()
                raise HTTPException(status_code=409, detail={"error": "no_active_session"})
            session_id = session_row['id']
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
        except:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
        return DiscardResponse(discarded_session_id=session_id)
    except HTTPException:
        raise
    except sqlite3.OperationalError:
        raise _503


def _do_put_delta(barcode: str, new_delta: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM sessions LIMIT 1").fetchone()
        if row is None:
            return None
        session_id = row["id"]
        cur = conn.execute(
            "UPDATE session_items SET delta = ? WHERE session_id = ? AND barcode = ?",
            (new_delta, session_id, barcode),
        )
        if cur.rowcount == 0:
            raise ValueError("item_not_found")
        total = conn.execute(
            "SELECT COALESCE(SUM(delta), 0) AS total FROM session_items WHERE session_id = ?",
            (session_id,),
        ).fetchone()["total"]
        conn.commit()
        return {"barcode": barcode, "delta": new_delta, "session_total_delta": total}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@router.put("/session/items/{barcode}")
async def put_session_item_delta(barcode: str, body: DeltaUpdateRequest):
    if body.delta < 0:
        raise HTTPException(status_code=400, detail={"error": "invalid_delta"})
    try:
        result = await asyncio.to_thread(_do_put_delta, barcode, body.delta)
    except ValueError as e:
        if str(e) == "item_not_found":
            raise HTTPException(status_code=404, detail={"error": "item_not_found"})
        raise
    if result is None:
        raise HTTPException(status_code=409, detail={"error": "no_active_session"})
    await manager.broadcast(json.dumps({
        "type": "delta_update",
        "barcode": result["barcode"],
        "session_delta": result["delta"],
        "session_total_delta": result["session_total_delta"],
    }))
    return result
