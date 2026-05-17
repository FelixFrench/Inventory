import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from src.api import reports as report_assembly
from src.api.dependencies import get_db

router = APIRouter()


@router.get("/reports/inventory")
def get_inventory(db: sqlite3.Connection = Depends(get_db)):
    try:
        return report_assembly.get_inventory_report(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/reports/low-stock")
def get_low_stock(db: sqlite3.Connection = Depends(get_db)):
    try:
        return report_assembly.get_low_stock_report(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
