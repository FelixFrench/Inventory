"""move_minimum_quantity_to_product_variants

Revision ID: 66c63d972d98
Revises: 1a63b54732eb
Create Date: 2026-07-05 19:50:14.660025

Sprint 2, Phase 1c. Relocates the per-product restock minimum from
``inventory.minimum_quantity`` to ``product_variants.minimum_quantity``, where the
v3.0.0 product-info UI (2c) and the product/group low-stock evaluation (2a/2b) will
read it. Behaviour-preserving: no report shape changes, no new features.

Native ADD/DROP COLUMN (the ``weight_g``-drop precedent, revision ``3001ecf62f32``),
NOT the 1b full-rebuild pattern: both tables are already composite-keyed and carry
FKs/CHECKs, so ``op.batch_alter_table`` would recreate the table and silently drop the
CHECK constraints. ``minimum_quantity`` is a plain column referenced by no index,
trigger, or view, so native DROP on the SQLite >= 3.35 targets (Pi 3.46.1) is safe and
leaves PK/FKs/indexes untouched.

Data preservation (orphan-carry): ``inventory`` rows exist for any confirmed barcode,
but ``product_variants`` rows exist only where OpenFoodFacts succeeded. A failed-OFF
barcode can therefore hold a non-zero ``inventory.minimum_quantity`` with no variant row
to receive it. A naive backfill-then-drop would silently discard that user-set minimum,
so step 4 creates a null-data carry variant row (``name``/``brand``/``product_quantity``
left NULL — all three are nullable) to hold it. This is the same null-data shape the
set-minimum upsert produces and is consistent with 3a's always-write-row direction. Such
a carry row makes its barcode surface in the unresolved report under ``missing`` labels
rather than the no-variant-row path (the I1 label shift arriving early via this path).

Idempotency / re-run safety: the ADD is guarded by column presence; the
backfill + orphan-carry + DROP block is gated on ``inventory`` still having the column,
so once the data move has completed (inventory column dropped) a re-run is a clean no-op
even though ``product_variants`` already has the column.

RECOVERY (SQLite DDL auto-commits and is not rolled back by Alembic on crash):
  1. MANDATORY pre-deploy, services stopped:  cp inventory.db inventory.db.bak
  2. If the migration crashes: restore inventory.db.bak and re-run ``alembic upgrade
     head`` (a restored backup re-runs from a clean old state; a partially-applied DB
     that still has the source column completes cleanly via the guarded block).

The downgrade is real and tested but NOT a perfect inverse: orphan-carry variant rows
created by ``upgrade()`` are not removed on ``downgrade()`` (they remain as null-data
variant rows once the minimum column is dropped). There is no marker distinguishing them
from OFF-created rows, so inventing one is out of scope; the artifact is documented
rather than detected-and-deleted.
"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '66c63d972d98'
down_revision: Union[str, Sequence[str], None] = '1a63b54732eb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Alembic revision modules get no logger for free; the pre-check line below is what a
# deploy reads to know how many null-data carry rows were created.
logger = logging.getLogger("alembic.runtime.migration")


def _has_column(conn, table: str, column: str) -> bool:
    rows = conn.execute(sa.text(f"PRAGMA table_info('{table}')")).fetchall()
    return any(r[1] == column for r in rows)


def upgrade() -> None:
    conn = op.get_bind()
    inv_has_min = _has_column(conn, 'inventory', 'minimum_quantity')
    pv_has_min = _has_column(conn, 'product_variants', 'minimum_quantity')

    # 1. Pre-check (log, don't fail): how many inventory minimums have no variant row.
    #    This is the number of null-data carry rows step 4 will create; 0 means the
    #    orphan path is inert on this data (as expected on the Pi).
    if inv_has_min:
        orphan_count = conn.execute(sa.text(
            "SELECT COUNT(*) FROM inventory i WHERE i.minimum_quantity > 0 "
            "AND NOT EXISTS (SELECT 1 FROM product_variants pv "
            "WHERE pv.barcode = i.barcode AND pv.retailer_id = i.retailer_id)"
        )).scalar_one()
        logger.info(
            "1c orphan-carry pre-check: %d inventory minimum(s) with no variant row",
            orphan_count,
        )

    # 2. ADD product_variants.minimum_quantity (guarded).
    if not pv_has_min:
        op.execute("ALTER TABLE product_variants ADD COLUMN minimum_quantity INTEGER NOT NULL DEFAULT 0")

    # 3-5 run only while the source column still exists (idempotent after a completed move).
    if inv_has_min:
        # 3. Backfill existing variants from inventory.
        op.execute(sa.text(
            "UPDATE product_variants SET minimum_quantity = "
            "(SELECT i.minimum_quantity FROM inventory i "
            " WHERE i.barcode = product_variants.barcode "
            "   AND i.retailer_id = product_variants.retailer_id) "
            "WHERE EXISTS (SELECT 1 FROM inventory i "
            " WHERE i.barcode = product_variants.barcode "
            "   AND i.retailer_id = product_variants.retailer_id)"
        ))
        # 4. Orphan-carry: preserve minimums that have no variant row by creating a
        #    null-data carry row. barcodes FK is satisfied because the inventory row
        #    already FKs barcodes.
        op.execute(sa.text(
            "INSERT INTO product_variants (barcode, retailer_id, minimum_quantity) "
            "SELECT i.barcode, i.retailer_id, i.minimum_quantity FROM inventory i "
            "WHERE i.minimum_quantity > 0 AND NOT EXISTS "
            "(SELECT 1 FROM product_variants pv "
            " WHERE pv.barcode = i.barcode AND pv.retailer_id = i.retailer_id)"
        ))
        # 5. DROP inventory.minimum_quantity.
        op.execute("ALTER TABLE inventory DROP COLUMN minimum_quantity")


def downgrade() -> None:
    conn = op.get_bind()

    # Restore the inventory column (guarded).
    if not _has_column(conn, 'inventory', 'minimum_quantity'):
        op.execute("ALTER TABLE inventory ADD COLUMN minimum_quantity INTEGER NOT NULL DEFAULT 0")

    # Backfill inventory from product_variants where the composite matches, then drop the
    # PV column. Not a perfect inverse: orphan-carry rows remain as null-data variant rows.
    if _has_column(conn, 'product_variants', 'minimum_quantity'):
        op.execute(sa.text(
            "UPDATE inventory SET minimum_quantity = "
            "(SELECT pv.minimum_quantity FROM product_variants pv "
            " WHERE pv.barcode = inventory.barcode "
            "   AND pv.retailer_id = inventory.retailer_id) "
            "WHERE EXISTS (SELECT 1 FROM product_variants pv "
            " WHERE pv.barcode = inventory.barcode "
            "   AND pv.retailer_id = inventory.retailer_id)"
        ))
        op.execute("ALTER TABLE product_variants DROP COLUMN minimum_quantity")
