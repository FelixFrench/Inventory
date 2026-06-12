import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from src.api.dependencies import get_db
from src.api.models import SetMinimumQuantityRequest
from src.api.reports import format_weight

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Products"])


@router.get("/products/minimum-quantities")
def get_minimum_quantities(request: Request, db: sqlite3.Connection = Depends(get_db)) -> dict:
    """
    List all inventory items with their current and minimum quantities.

    Returns product name, brand, weight, current stock, and minimum stock threshold
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
                pv.weight_g
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
                "weight": format_weight(row["weight_g"]),
                "current_quantity": row["current_quantity"],
                "minimum_quantity": row["minimum_quantity"],
            }
            for row in rows
        ]
        return {"products": products}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("DB error fetching minimum quantities: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/products/{barcode}/minimum_quantity")
def set_minimum_quantity(
    barcode: str,
    body: SetMinimumQuantityRequest,
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
) -> dict | JSONResponse:
    """
    Set the minimum quantity threshold for a product.

    Updates the restock alert level for the given barcode. Returns 400 if the
    supplied minimum_quantity is negative, 404 if the barcode does not exist in
    inventory.
    """
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
    except Exception as e:
        logger.error("DB error updating minimum_quantity for %s: %s", barcode, e)
        raise HTTPException(status_code=500, detail="Internal server error")
