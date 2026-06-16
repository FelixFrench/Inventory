"""Shared error responses for API routers.

`SERVICE_UNAVAILABLE_503` mirrors the local `_503` defined in scan.py/session.py
(those remain out of scope for Phase 7d and keep their own copies); this shared
copy is imported by the reports and products routers so they map SQLite write
contention to a 503 + Retry-After consistently.
"""

from fastapi import HTTPException

SERVICE_UNAVAILABLE_503 = HTTPException(
    status_code=503,
    detail="Service temporarily unavailable",
    headers={"Retry-After": "1"},
)
