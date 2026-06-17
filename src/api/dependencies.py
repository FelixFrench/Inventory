import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Cookie, Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from src.db.db import get_connection

load_dotenv(Path(__file__).parents[2] / "config.local.env")
_API_KEY = os.environ.get("INVENTORY_API_KEY")

logger = logging.getLogger(__name__)

_api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_db():
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


async def verify_api_key(api_key: str | None = Depends(_api_key_scheme)):
    if _API_KEY is None:
        raise RuntimeError("INVENTORY_API_KEY not set in environment or config.local.env")
    if api_key is None or not secrets.compare_digest(api_key, _API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def verify_docs_access(
    api_key: str | None = Depends(_api_key_scheme),
    docs_session: str | None = Cookie(default=None),
) -> None:
    if _API_KEY is None:
        raise RuntimeError("INVENTORY_API_KEY not set in environment or config.local.env")
    if (api_key is not None and secrets.compare_digest(api_key, _API_KEY)) or (
        docs_session is not None and secrets.compare_digest(docs_session, _API_KEY)
    ):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


def get_retailer_id(request: Request) -> int:
    return request.app.state.sainsburys_retailer_id
