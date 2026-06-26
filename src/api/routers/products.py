import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from src.api.dependencies import get_db
from src.api.errors import SERVICE_UNAVAILABLE_503 as _503
from src.api.models import SetMinimumQuantityRequest

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
                COALESCE(inv.quantity, 0)         AS current_quantity,
                COALESCE(inv.minimum_quantity, 0) AS minimum_quantity,
                pv.name,
                pv.brand,
                pv.product_quantity
            FROM inventory inv
            LEFT JOIN product_variants pv
                ON pv.barcode = inv.barcode
                AND pv.retailer_id = ?
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
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
) -> dict | JSONResponse:
    """
    Set the minimum quantity threshold for a product.

    Updates the restock alert level for the given barcode. A negative
    minimum_quantity is rejected at the schema layer (422,
    SetMinimumQuantityRequest.minimum_quantity has ge=0). Returns 404 if the
    barcode does not exist in inventory.
    """
    # Defence in depth: schema validation (Field(ge=0)) already rejects negatives
    # with 422 before this handler runs, so this branch is not reachable via HTTP.
    if body.minimum_quantity < 0:
        return JSONResponse(status_code=400, content={"error": "invalid_minimum_quantity"})
    try:
        cursor = db.execute(
            "UPDATE inventory SET minimum_quantity = ? WHERE barcode = ?",
            (body.minimum_quantity, barcode),
        )
        db.commit()
        if cursor.rowcount == 0:
            return JSONResponse(status_code=404, content={"error": "product_not_found"})
        return {"barcode": barcode, "minimum_quantity": body.minimum_quantity}
    except HTTPException:
        raise
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error updating minimum_quantity for %s: %s", barcode, e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})
