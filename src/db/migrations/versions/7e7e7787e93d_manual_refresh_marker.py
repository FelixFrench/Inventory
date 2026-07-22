"""manual_refresh_marker

Revision ID: 7e7e7787e93d
Revises: a7d2f4e9c1b8
Create Date: 2026-07-22 00:00:00.000000

Sprint 2, Phase 3c. Adds a single request-marker column to ``product_variants``:

  * ``manual_refresh_requested INTEGER NOT NULL DEFAULT 0 CHECK(manual_refresh_requested IN (0,1))``

The manual-refresh REST endpoint (``POST /products/{barcode}/{retailer_id}/refresh``) sets this to 1
and returns immediately; FastAPI never calls OpenFoodFacts. The single worker process selects marked
rows on its next poll (between the live-scan polls and the 3b background scheduler), performs the paced
OFF call + chained price lookup, writes the durable cache rows, and clears the marker (back to 0) in
the SAME transaction as the durable OFF write. This preserves the single-OFF-caller invariant from
3a/3b. The marker is read/written ONLY by the manual-refresh endpoint and worker path; it is entirely
separate from ``session_items``.

Native ADD/DROP COLUMN (the ``minimum_quantity`` / ``durable_lookup_state`` precedents), NOT
``batch_alter_table`` which would silently drop ``product_variants``' FKs/CHECKs. The new column is
referenced by no index/trigger/view, so a native DROP on SQLite >= 3.35 (Pi 3.46.1) is safe.

Idempotency / crash-safety: the ADD COLUMN is guarded by a ``PRAGMA table_info`` presence check, so a
partial apply is safe to re-run. The CHECK is satisfied by the ``DEFAULT 0``, so ADD COLUMN succeeds
against preexisting rows (validated on SQLite >= 3.37). No backfill statement is needed (the DEFAULT
populates existing rows); a guarded re-apply therefore cannot re-zero a marker set between a crash and
the recovery re-run. The CHECK is not visible to ``PRAGMA``, so it is hand-carried into
``current_schema.sql``.

RECOVERY (SQLite DDL auto-commits and is not rolled back by Alembic on crash):
  1. MANDATORY pre-deploy, services stopped:  cp inventory.db inventory.db.bak
  2. If the migration crashes: restore inventory.db.bak and re-run ``alembic upgrade head``. If the
     column exists but ``alembic_version`` was not advanced, stamp manually:
       sqlite3 inventory.db "UPDATE alembic_version SET version_num='7e7e7787e93d';"

Downgrade is a real, tested inverse: drop the column (guarded). The column-level CHECK drops with it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7e7e7787e93d'
down_revision: Union[str, Sequence[str], None] = 'a7d2f4e9c1b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The CHECK is written inline in the column definition (not table-level) so DROP COLUMN in downgrade()
# removes it with the column. The DEFAULT 0 satisfies the CHECK for preexisting rows on ADD COLUMN.
_COLUMN = (
    "manual_refresh_requested INTEGER NOT NULL DEFAULT 0 "
    "CHECK(manual_refresh_requested IN (0, 1))"
)


def _has_column(conn, table: str, column: str) -> bool:
    rows = conn.execute(sa.text(f"PRAGMA table_info('{table}')")).fetchall()
    return any(r[1] == column for r in rows)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, 'product_variants', 'manual_refresh_requested'):
        op.execute(f"ALTER TABLE product_variants ADD COLUMN {_COLUMN}")


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, 'product_variants', 'manual_refresh_requested'):
        op.execute("ALTER TABLE product_variants DROP COLUMN manual_refresh_requested")
