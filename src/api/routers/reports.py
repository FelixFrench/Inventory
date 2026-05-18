import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from src.api import reports as report_assembly
from src.api.dependencies import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/reports/inventory")
def get_inventory(db: sqlite3.Connection = Depends(get_db)):
    try:
        return report_assembly.get_inventory_report(db)
    except Exception as e:
        logger.error("DB error fetching inventory report: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/reports/low-stock")
def get_low_stock(db: sqlite3.Connection = Depends(get_db)):
    try:
        return report_assembly.get_low_stock_report(db)
    except Exception as e:
        logger.error("DB error fetching low-stock report: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")
