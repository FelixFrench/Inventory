"""drop_legacy_scan_events_and_config

Revision ID: 605be7ba628c
Revises: 3001ecf62f32
Create Date: 2026-06-26

Drops the two dead v1 carry-over tables: scan_events (unwritten since v1 scan.py)
and config (unused KV store). Neither is referenced by v2 code, and no other
table's FK references them, so they are safe leaf drops.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = '605be7ba628c'
down_revision: Union[str, Sequence[str], None] = '3001ecf62f32'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # DROP TABLE IF EXISTS is idempotent and auto-removes any associated
    # indexes/triggers, so no PRAGMA guard is needed.
    op.execute(text("DROP TABLE IF EXISTS scan_events"))
    op.execute(text("DROP TABLE IF EXISTS config"))


def downgrade() -> None:
    # Recreate the table structures verbatim from the frozen initial_schema.sql
    # (idempotent form). Row data is NOT recoverable — these tables were unused,
    # so this is acceptable.
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS scan_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            barcode     TEXT    NOT NULL,
            retailer_id INTEGER REFERENCES retailers(id),
            direction   TEXT    NOT NULL CHECK(direction IN ('in', 'out')),
            timestamp   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """))
