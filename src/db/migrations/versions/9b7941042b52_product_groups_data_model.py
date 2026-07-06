"""product_groups_data_model

Revision ID: 9b7941042b52
Revises: 66c63d972d98
Create Date: 2026-07-06 18:17:07.720094

Sprint 2, Phase 2a. Lands the durable product-groups data model: a ``product_groups``
entity table and two edge tables (Option B, two-table polymorphic membership) so that a
group's members are variants ``(barcode, retailer_id)`` and/or other groups. Data-layer
only -- no HTTP surface, no UI (2b/2c own those).

Shape (two edge tables, real non-null FKs, DB-level self-edge CHECK):
  * ``product_groups(id, name UNIQUE, minimum_quantity)`` -- ``0`` = organisational/no minimum.
  * ``group_variant_members(group_id, barcode, retailer_id)`` -- composite FK to
    ``product_variants(barcode, retailer_id)`` (PK order copied verbatim from the head
    schema; a mismatched column order raises ``foreign key mismatch`` at insert time).
  * ``group_group_members(parent_group_id, child_group_id)`` -- both FK ``product_groups``,
    with ``CHECK(parent_group_id != child_group_id)`` for cheap DB-level self-edge rejection.

Cascade -- edges only, from both sides. The only ``ON DELETE CASCADE`` FKs live on the two
edge tables and all point *up* to the entity tables, so a delete can only ever remove edge
rows, never another group or a variant:
  * Deleting a ``product_groups`` row drops the edges where it is the parent
    (``group_variant_members.group_id``, ``group_group_members.parent_group_id``) *and* the
    edges where it is a child group (``group_group_members.child_group_id``). The child
    group / member variants themselves survive; the deleted group simply disappears from any
    parent.
  * Deleting a ``product_variants`` row drops the ``group_variant_members`` edges that
    reference it (composite FK cascade).
No cascade path reaches an entity table.

Indexes: none created explicitly. The recursive resolver joins on the parent side
(``group_group_members.parent_group_id`` and ``group_variant_members.group_id``), each the
leftmost column of its table's composite PRIMARY KEY, so the PK auto-index already covers
the walk. An extra ``CREATE INDEX`` on the same prefix would be dead weight on a tiny
single-user dataset; ``PRAGMA index_list`` shows the covering PK auto-index (``origin='pk'``).

Idempotency / crash-safety: DDL is ``CREATE TABLE IF NOT EXISTS`` only (no ``ADD COLUMN``),
so a partial apply is safe to re-run -- already-created tables are skipped and the rest is
completed. ``PRAGMA foreign_keys`` is per-connection and ignored inside the migration;
cascade enforcement is a runtime property of the app/test connection.

RECOVERY (SQLite DDL auto-commits and is not rolled back by Alembic on crash):
  1. MANDATORY pre-deploy, services stopped:  cp inventory.db inventory.db.bak
  2. If the migration crashes: re-run ``alembic upgrade head`` (IF NOT EXISTS makes the
     already-created tables a no-op and finishes the rest). If the tables exist but
     ``alembic_version`` was not advanced, stamp manually:
       sqlite3 inventory.db "UPDATE alembic_version SET version_num='9b7941042b52';"

Downgrade is a real, tested inverse: drop the edge (child) tables before ``product_groups``
(parent) to respect FKs; dropping a table drops its auto-indexes.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9b7941042b52'
down_revision: Union[str, Sequence[str], None] = '66c63d972d98'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Separate op.execute() per statement (sidesteps the comment/';'-split trap). All CREATE
    # ... IF NOT EXISTS, so a re-run after a partial apply is a safe no-op.
    op.execute(
        "CREATE TABLE IF NOT EXISTS product_groups ("
        " id               INTEGER PRIMARY KEY,"
        " name             TEXT    NOT NULL UNIQUE,"
        " minimum_quantity INTEGER NOT NULL DEFAULT 0 CHECK(minimum_quantity >= 0)"
        ")"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS group_variant_members ("
        " group_id    INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE,"
        " barcode     TEXT    NOT NULL,"
        " retailer_id INTEGER NOT NULL,"
        " PRIMARY KEY (group_id, barcode, retailer_id),"
        " FOREIGN KEY (barcode, retailer_id)"
        "     REFERENCES product_variants(barcode, retailer_id) ON DELETE CASCADE"
        ")"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS group_group_members ("
        " parent_group_id INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE,"
        " child_group_id  INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE,"
        " PRIMARY KEY (parent_group_id, child_group_id),"
        " CHECK (parent_group_id != child_group_id)"
        ")"
    )


def downgrade() -> None:
    # Child (edge) tables first, then the parent entity table. IF EXISTS keeps a partial
    # downgrade re-runnable.
    op.execute("DROP TABLE IF EXISTS group_group_members")
    op.execute("DROP TABLE IF EXISTS group_variant_members")
    op.execute("DROP TABLE IF EXISTS product_groups")
