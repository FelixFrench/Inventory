import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from src.api import reports as report_assembly
from src.api.dependencies import get_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Reports"])


def _get_retailer_id(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT id FROM retailers WHERE name = 'Sainsbury''s'").fetchone()
    if row is None:
        raise HTTPException(status_code=500, detail="Sainsbury's retailer not configured")
    return row['id']


@router.get("/reports/inventory")
def get_inventory(db: sqlite3.Connection = Depends(get_db)) -> dict:
    """
    Return the full inventory report.

    Lists all tracked items with current quantities, product details, and prices.
    """
    try:
        retailer_id = _get_retailer_id(db)
        return report_assembly.get_inventory_report(db, retailer_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("DB error fetching inventory report: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/reports/low-stock")
def get_low_stock(db: sqlite3.Connection = Depends(get_db)) -> dict:
    """
    Return items that are at or below their minimum quantity threshold.

    Includes current quantity, minimum quantity, and shortfall for each item.
    """
    try:
        retailer_id = _get_retailer_id(db)
        return report_assembly.get_low_stock_report(db, retailer_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("DB error fetching low-stock report: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/reports/unresolved")
def get_unresolved(request: Request, db: sqlite3.Connection = Depends(get_db)) -> dict:
    """
    Return items still awaiting product info or price lookups.

    Lists barcodes whose info_status or price_status has not yet been resolved by
    the background worker.
    """
    retailer_id = request.app.state.sainsburys_retailer_id
    return report_assembly.get_unresolved_report(db, retailer_id)
