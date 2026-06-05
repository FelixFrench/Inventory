import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from src.api import printer
from src.api.printer import PrinterUnavailableError
from src.api.dependencies import get_db
from src.api.reports import get_inventory_report, get_low_stock_report

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/print")


@router.post("/inventory")
def post_print_inventory(request: Request, db=Depends(get_db)):
    retailer_id = request.app.state.sainsburys_retailer_id
    data = get_inventory_report(db, retailer_id)
    try:
        printer.print_inventory(data)
    except PrinterUnavailableError as e:
        logger.warning("Print inventory failed: %s", e)
        return JSONResponse(status_code=503, content={"error": e.code})
    return {"printed": True}


@router.post("/low-stock")
def post_print_low_stock(request: Request, db=Depends(get_db)):
    retailer_id = request.app.state.sainsburys_retailer_id
    data = get_low_stock_report(db, retailer_id)
    try:
        printer.print_low_stock(data)
    except PrinterUnavailableError as e:
        logger.warning("Print low-stock failed: %s", e)
        return JSONResponse(status_code=503, content={"error": e.code})
    return {"printed": True}
