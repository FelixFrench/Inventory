import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from src.api.dependencies import get_db, get_retailer_id
from src.api.errors import SERVICE_UNAVAILABLE_503 as _503
from src.api.models import AddGroupMembershipRequest, SetMinimumQuantityRequest
from src.api.routers.groups import (
    add_variant_to_group,
    remove_variant_from_group,
    run_membership,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Products"])


@router.get("/products/minimum-quantities", response_model=None)
def get_minimum_quantities(request: Request, db: sqlite3.Connection = Depends(get_db)) -> dict | JSONResponse:
    """
    List all inventory items with their current and minimum quantities.

    Returns product name, brand, quantity, current stock, and minimum stock threshold
    for every item in inventory, ordered alphabetically by name.
    """
    try:
        retailer_id = request.app.state.sainsburys_retailer_id
        rows = db.execute(
            """
            SELECT
                inv.barcode,
                COALESCE(inv.quantity, 0)        AS current_quantity,
                COALESCE(pv.minimum_quantity, 0) AS minimum_quantity,
                pv.name,
                pv.brand,
                pv.product_quantity
            FROM inventory inv
            LEFT JOIN product_variants pv
                ON pv.barcode = inv.barcode
                AND pv.retailer_id = inv.retailer_id
            WHERE inv.retailer_id = ?
            ORDER BY pv.name ASC NULLS LAST, inv.barcode ASC
            """,
            (retailer_id,),
        ).fetchall()
        products = [
            {
                "barcode": row["barcode"],
                "name": row["name"],
                "brand": row["brand"],
                "quantity": row["product_quantity"],
                "current_quantity": row["current_quantity"],
                "minimum_quantity": row["minimum_quantity"],
            }
            for row in rows
        ]
        return {"products": products}
    except HTTPException:
        raise
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error fetching minimum quantities: %s", e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})


@router.put("/products/{barcode}/minimum_quantity", response_model=None)
def set_minimum_quantity(
    barcode: str,
    body: SetMinimumQuantityRequest,
    db: sqlite3.Connection = Depends(get_db),
    retailer_id: int = Depends(get_retailer_id),
) -> dict | JSONResponse:
    """
    Set the minimum quantity threshold for a product.

    Upserts the restock alert level onto product_variants for (barcode, retailer_id):
    updates the row if it exists, otherwise creates a null-data variant row carrying
    only the minimum. A negative minimum_quantity is rejected at the schema layer (422,
    SetMinimumQuantityRequest.minimum_quantity has ge=0). The minimum is settable for any
    in-system barcode (one with a barcodes row); a barcode with no barcodes row violates
    the FK and returns 404.
    """
    # Defence in depth: schema validation (Field(ge=0)) already rejects negatives
    # with 422 before this handler runs, so this branch is not reachable via HTTP.
    if body.minimum_quantity < 0:
        return JSONResponse(status_code=400, content={"error": "invalid_minimum_quantity"})
    try:
        db.execute(
            "INSERT INTO product_variants (barcode, retailer_id, minimum_quantity) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(barcode, retailer_id) DO UPDATE SET minimum_quantity = excluded.minimum_quantity",
            (barcode, retailer_id, body.minimum_quantity),
        )
        db.commit()
        return {"barcode": barcode, "minimum_quantity": body.minimum_quantity}
    except sqlite3.IntegrityError:
        db.rollback()  # release the implicit transaction from the failed FK insert
        return JSONResponse(status_code=404, content={"error": "barcode_not_found"})
    except HTTPException:
        raise
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error updating minimum_quantity for %s: %s", barcode, e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})


@router.get("/products/{barcode}", response_model=None)
def get_product_detail(
    barcode: str,
    db: sqlite3.Connection = Depends(get_db),
    retailer_id: int = Depends(get_retailer_id),
) -> dict | JSONResponse:
    """
    Return one variant's own fields, current inventory quantity, minimum, price fields,
    and the groups it is directly a member of.

    LEFT JOINs throughout so a null-data / unresolved variant still renders (null name/brand/
    quantity, no price row). A barcode absent from `barcodes` entirely -> 404 barcode_not_found.
    Returns raw nullable fields; the frontend (2c) decides display text. No lookup-status
    field -- the durable status columns do not exist until 3a.
    """
    try:
        row = db.execute(
            """
            SELECT
                b.barcode,
                pv.name,
                pv.brand,
                pv.product_quantity,
                COALESCE(pv.minimum_quantity, 0) AS minimum_quantity,
                COALESCE(inv.quantity, 0)        AS current_quantity,
                pr.price_pence,
                pr.price_type,
                pr.product_url
            FROM barcodes b
            LEFT JOIN product_variants pv
                   ON pv.barcode = b.barcode AND pv.retailer_id = ?
            LEFT JOIN inventory inv
                   ON inv.barcode = b.barcode AND inv.retailer_id = ?
            LEFT JOIN prices pr
                   ON pr.barcode = b.barcode AND pr.retailer_id = ?
            WHERE b.barcode = ?
            """,
            (retailer_id, retailer_id, retailer_id, barcode),
        ).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "barcode_not_found"})

        group_rows = db.execute(
            "SELECT pg.id, pg.name FROM group_variant_members gvm "
            "JOIN product_groups pg ON pg.id = gvm.group_id "
            "WHERE gvm.barcode = ? AND gvm.retailer_id = ? ORDER BY pg.name ASC",
            (barcode, retailer_id),
        ).fetchall()

        return {
            "barcode": row["barcode"],
            "name": row["name"],
            "brand": row["brand"],
            "product_quantity": row["product_quantity"],
            "minimum_quantity": row["minimum_quantity"],
            "current_quantity": row["current_quantity"],
            "price_pence": row["price_pence"],
            "price_type": row["price_type"],
            "product_url": row["product_url"],
            "groups": [{"id": r["id"], "name": r["name"]} for r in group_rows],
        }
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error fetching product detail for %s: %s", barcode, e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})


@router.post("/products/{barcode}/groups", response_model=None)
def add_product_to_group(
    barcode: str,
    body: AddGroupMembershipRequest,
    db: sqlite3.Connection = Depends(get_db),
    retailer_id: int = Depends(get_retailer_id),
) -> dict | JSONResponse:
    """Product-facing membership add: put this variant into a group. Drives the same edge
    logic as the group-side endpoint (upsert-then-insert, idempotent)."""
    return run_membership(
        db, lambda: add_variant_to_group(db, body.group_id, barcode, retailer_id)
    )


@router.delete("/products/{barcode}/groups/{group_id}", response_model=None)
def remove_product_from_group(
    barcode: str,
    group_id: int,
    db: sqlite3.Connection = Depends(get_db),
    retailer_id: int = Depends(get_retailer_id),
) -> dict | JSONResponse:
    """Product-facing membership remove. Validates both the barcode and the group exist
    (symmetric with the group side); a valid non-membership is an idempotent no-op."""
    return run_membership(
        db, lambda: remove_variant_from_group(db, group_id, barcode, retailer_id)
    )
