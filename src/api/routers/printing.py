import logging
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from src.api import printer
from src.api.printer import PrinterUnavailableError
from src.api.dependencies import get_db
from src.api.errors import SERVICE_UNAVAILABLE_503 as _503
from src.api.reports import get_inventory_report, get_low_stock_report
from src.api.routers.products import _retailer_missing_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/print", tags=["Print"])


@router.post("/inventory", response_model=None)
def post_print_inventory(request: Request, db=Depends(get_db)) -> dict | JSONResponse:
    """
    Print the full inventory report to the configured receipt printer.

    Returns `{printed: true}` on success. Returns 503 if the printer is not
    configured or is unreachable.
    """
    retailer_id = request.app.state.sainsburys_retailer_id
    data = get_inventory_report(db, retailer_id)
    try:
        printer.print_inventory(data)
    except PrinterUnavailableError as e:
        logger.warning("Print inventory failed: %s", e)
        return JSONResponse(status_code=503, content={"error": e.code})
    return {"printed": True}


@router.post("/low-stock", response_model=None)
def post_print_low_stock(request: Request, db=Depends(get_db)) -> dict | JSONResponse:
    """
    Print the low-stock report to the configured receipt printer.

    Returns `{printed: true}` on success. Returns 503 if the printer is not
    configured or is unreachable.
    """
    retailer_id = request.app.state.sainsburys_retailer_id
    data = get_low_stock_report(db, retailer_id)
    try:
        printer.print_low_stock(data)
    except PrinterUnavailableError as e:
        logger.warning("Print low-stock failed: %s", e)
        return JSONResponse(status_code=503, content={"error": e.code})
    return {"printed": True}


@router.post("/product/{barcode}/{retailer_id}", response_model=None)
def post_print_product(
    barcode: str,
    retailer_id: int,
    db: sqlite3.Connection = Depends(get_db),
) -> dict | JSONResponse:
    """
    Print one product variant's name/brand/quantity and a scannable barcode as a
    fragment, without cutting the paper, so several products can be printed onto
    one strip before a single Cut.

    The variant is identified by the composite (barcode, retailer_id) from the path,
    the same convention as GET /products/{barcode}/{retailer_id}. Fields are sourced
    server-side from product_variants, never trusted from the client. An unknown
    retailer_id -> 404 retailer_not_found; a barcode absent from `barcodes` entirely
    -> 404 barcode_not_found. Returns 503 printer_not_configured if PRINTER_IP is unset.
    """
    try:
        guard = _retailer_missing_response(db, retailer_id)
        if guard is not None:
            return guard
        row = db.execute(
            """
            SELECT b.barcode, pv.name, pv.brand, pv.product_quantity
            FROM barcodes b
            LEFT JOIN product_variants pv
                   ON pv.barcode = b.barcode AND pv.retailer_id = ?
            WHERE b.barcode = ?
            """,
            (retailer_id, barcode),
        ).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "barcode_not_found"})
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error fetching product for print %s: %s", barcode, e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    product = {
        "barcode": row["barcode"],
        "name": row["name"],
        "brand": row["brand"],
        "quantity": row["product_quantity"],
    }
    try:
        printer.print_product(product)
    except PrinterUnavailableError as e:
        logger.warning("Print product failed: %s", e)
        return JSONResponse(status_code=503, content={"error": e.code})
    return {"printed": True}


@router.post("/cut", response_model=None)
def post_print_cut() -> dict | JSONResponse:
    """
    Issue a paper cut on the configured receipt printer, without printing any
    product content. Used after one or more Print calls to cut the accumulated strip.

    Returns 503 if the printer is not configured or is unreachable.
    """
    try:
        printer.print_cut()
    except PrinterUnavailableError as e:
        logger.warning("Print cut failed: %s", e)
        return JSONResponse(status_code=503, content={"error": e.code})
    return {"printed": True}
