"""durable_lookup_state

Revision ID: a7d2f4e9c1b8
Revises: 9b7941042b52
Create Date: 2026-07-21 00:00:00.000000

Sprint 2, Phase 3a. Adds the durable lookup-state substrate that the 3b/3c refresh/retry
scheduler will read. Three columns are added to BOTH cache tables ``product_variants`` (the
OFF side) and ``prices`` (the price side):

  * ``lookup_status TEXT NOT NULL DEFAULT 'pending' CHECK(lookup_status IN ('pending','resolved','failed'))``
  * ``lookup_failure_count INTEGER NOT NULL DEFAULT 0 CHECK(lookup_failure_count >= 0)``
  * ``last_lookup_datetime TEXT`` (nullable; ISO-8601 text, 1-second resolution, same style as
    ``worker_state.off_last_called_at``)

These are read ONLY by the refresh worker (3b+); they are entirely separate from
``session_items.info_status`` / ``price_status`` (which remain the sole confirm-gate input).

State semantics:
  * OFF side (``product_variants.lookup_status``): ``pending`` = no OFF lookup reached a terminal
    outcome (e.g. an upsert-created null-data carry row); ``resolved`` = OFF succeeded and data
    was written; ``failed`` = OFF was attempted and failed.
  * Price side (``prices.lookup_status``): ``resolved`` = a price lookup succeeded; ``failed`` =
    a price lookup was attempted and failed. ``pending`` is permitted by the CHECK for
    forward-compat but is never written to a ``prices`` row by 3a (a ``prices`` row is only ever
    written after a price attempt). "Price never attempted" is encoded by the ABSENCE of a
    ``prices`` row.

Backfill (runs ONLY on first application — gated on ``lookup_status`` being absent at entry — so
a guarded re-apply after a crash cannot reset a legitimately-``failed`` row back to ``resolved``):
  * ``product_variants``: rows with a non-null ``name`` -> ``lookup_status='resolved'`` (they
    exist because OFF succeeded historically); null-name rows keep the ``'pending'`` default
    (upsert/carry rows OFF never resolved). ``lookup_failure_count`` stays 0.
  * ``prices``: all existing rows -> ``lookup_status='resolved'`` (verified against the live
    worker: a ``prices`` row is written only on a successful price lookup today; price failure
    stamps ``session_items`` only and writes no row).
  * ``last_lookup_datetime``: left NULL for all backfilled rows. No pre-existing cache-timestamp
    column exists on either table (the V1.0 ``last_updated`` columns were removed at revision
    ``0fcacd84ad0a``), and stamping "now" would defeat 3b's staleness cadence. 3b MUST treat
    NULL as maximally-stale.

Native ADD/DROP COLUMN (the ``minimum_quantity`` precedent, revision ``66c63d972d98``), NOT the
full-rebuild pattern: both tables carry FKs/CHECKs that ``op.batch_alter_table`` would silently
drop. The new columns are referenced by no index/trigger/view, so native DROP on SQLite >= 3.35
(Pi 3.46.1) is safe and leaves PK/FKs/existing indexes untouched.

Idempotency / crash-safety: each ADD COLUMN is guarded by its own ``PRAGMA table_info`` presence
check, so a partial apply is safe to re-run. The CHECKs are satisfied by the DEFAULTs, so ADD
COLUMN succeeds against preexisting rows (validated on SQLite >= 3.37). CHECK constraints are not
visible to ``PRAGMA``, so they are hand-carried into ``current_schema.sql``.

RECOVERY (SQLite DDL auto-commits and is not rolled back by Alembic on crash):
  1. MANDATORY pre-deploy, services stopped:  cp inventory.db inventory.db.bak
  2. If the migration crashes: restore inventory.db.bak and re-run ``alembic upgrade head`` (a
     restored backup re-runs the backfill from a clean state). If the columns exist but
     ``alembic_version`` was not advanced, stamp manually:
       sqlite3 inventory.db "UPDATE alembic_version SET version_num='a7d2f4e9c1b8';"

Downgrade is a real, tested inverse: drop the six columns (guarded). Dropping a column drops any
data in it; backfilled ``lookup_status`` values are not otherwise recoverable, which is expected.
"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7d2f4e9c1b8'
down_revision: Union[str, Sequence[str], None] = '9b7941042b52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

# (column_name, column_definition) for the three durable columns. CHECKs are written inline in
# the column definition (not table-level) so DROP COLUMN in downgrade() removes each CHECK with
# its column.
_COLUMNS = (
    ("lookup_status",
     "lookup_status TEXT NOT NULL DEFAULT 'pending' CHECK(lookup_status IN ('pending','resolved','failed'))"),
    ("lookup_failure_count",
     "lookup_failure_count INTEGER NOT NULL DEFAULT 0 CHECK(lookup_failure_count >= 0)"),
    ("last_lookup_datetime",
     "last_lookup_datetime TEXT"),
)


def _has_column(conn, table: str, column: str) -> bool:
    rows = conn.execute(sa.text(f"PRAGMA table_info('{table}')")).fetchall()
    return any(r[1] == column for r in rows)


def upgrade() -> None:
    conn = op.get_bind()

    for table in ('product_variants', 'prices'):
        # Capture whether the columns already existed BEFORE adding, so the backfill runs only on
        # a genuine first application (columns absent at entry) and never on a guarded re-apply.
        had_columns = _has_column(conn, table, 'lookup_status')

        # Each ADD COLUMN individually guarded (crash-safe re-run). Defaults satisfy the CHECKs,
        # so the ADD succeeds against preexisting rows.
        for column, definition in _COLUMNS:
            if not _has_column(conn, table, column):
                op.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

        if not had_columns:
            if table == 'product_variants':
                # OFF succeeded historically iff a name was stored; null-name rows are
                # upsert/carry rows OFF never resolved and keep the 'pending' default.
                count = conn.execute(sa.text(
                    "SELECT COUNT(*) FROM product_variants WHERE name IS NOT NULL"
                )).scalar_one()
                op.execute(
                    "UPDATE product_variants SET lookup_status = 'resolved' WHERE name IS NOT NULL"
                )
            else:
                # Every existing prices row implies a past successful price lookup.
                count = conn.execute(sa.text("SELECT COUNT(*) FROM prices")).scalar_one()
                op.execute("UPDATE prices SET lookup_status = 'resolved'")
            logger.info("3a backfill: %s.lookup_status='resolved' on %d row(s)", table, count)


def downgrade() -> None:
    conn = op.get_bind()
    for table in ('product_variants', 'prices'):
        for column, _definition in _COLUMNS:
            if _has_column(conn, table, column):
                op.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
