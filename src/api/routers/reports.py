import logging
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from src.api import reports as report_assembly
from src.api.dependencies import get_db
from src.api.errors import SERVICE_UNAVAILABLE_503 as _503

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Reports"])


@router.get("/reports/inventory", response_model=None)
def get_inventory(request: Request, db: sqlite3.Connection = Depends(get_db)) -> dict | JSONResponse:
    """
    Return the full inventory report.

    Lists all tracked items with current quantities, product details, and prices.
    """
    retailer_id = request.app.state.sainsburys_retailer_id
    try:
        return report_assembly.get_inventory_report(db, retailer_id)
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error fetching inventory report: %s", e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})


@router.get("/reports/low-stock", response_model=None)
def get_low_stock(request: Request, db: sqlite3.Connection = Depends(get_db)) -> dict | JSONResponse:
    """
    Return items that are at or below their minimum quantity threshold.

    Includes current quantity, minimum quantity, and shortfall for each item.
    """
    retailer_id = request.app.state.sainsburys_retailer_id
    try:
        return report_assembly.get_low_stock_report(db, retailer_id)
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error fetching low-stock report: %s", e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})


@router.get("/reports/unresolved", response_model=None)
def get_unresolved(request: Request, db: sqlite3.Connection = Depends(get_db)) -> dict | JSONResponse:
    """
    Return items still awaiting product info or price lookups.

    Lists barcodes whose info_status or price_status has not yet been resolved by
    the background worker.
    """
    retailer_id = request.app.state.sainsburys_retailer_id
    try:
        return report_assembly.get_unresolved_report(db, retailer_id)
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error fetching unresolved report: %s", e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})
