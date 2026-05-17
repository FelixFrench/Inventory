import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Header, HTTPException

from src.db.db import get_connection

load_dotenv(Path(__file__).parents[2] / "config.local.env")
_API_KEY = os.environ.get("INVENTORY_API_KEY")

logger = logging.getLogger(__name__)


def get_db():
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def verify_api_key(x_api_key: str = Header(None)):
    if _API_KEY is None:
        raise RuntimeError("INVENTORY_API_KEY not set in environment or config.local.env")
    if x_api_key != _API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
