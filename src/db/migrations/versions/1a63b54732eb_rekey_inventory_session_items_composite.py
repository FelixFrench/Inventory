"""rekey_inventory_session_items_composite

Revision ID: 1a63b54732eb
Revises: 605be7ba628c
Create Date: 2026-07-04 21:05:42.470069

Sprint 2, Phase 1b. Re-keys ``inventory`` to PK ``(barcode, retailer_id)`` and
``session_items`` to PK ``(session_id, barcode, retailer_id)``, and replaces the two
separate FKs on ``prices`` with a single composite FK to
``product_variants(barcode, retailer_id)``. ``retailer_id`` on the two re-keyed tables
is backfilled to the Sainsbury's id (resolved by name from ``retailers`` — the only
retailer seeded this sprint; see ``initial_schema.sql``).

Uses the manual create-new -> copy -> drop-old -> rename pattern under
``PRAGMA foreign_keys = OFF`` (the precedent from revision ``0fcacd84ad0a``). Alembic's
migrator connection never issues ``PRAGMA foreign_keys = ON`` (unlike the runtime
``db.py`` connection), so FK enforcement is already off during migration; the bracketing
below mirrors the precedent, and ``PRAGMA foreign_key_check`` at the end is the real
safety net.

Deliberately NOT FK'd to ``product_variants``: a confirmed scan-in of a barcode whose
OpenFoodFacts lookup failed creates an ``inventory``/``session_items`` row with no
``product_variants`` row, so such an FK would be violated. ``prices`` may FK
``product_variants`` because a price row only exists after a successful OFF lookup.

Idempotency / re-run safety: the top-of-upgrade guard inspects whether ``retailer_id``
is present on both re-keyed tables and no-ops a fully-applied DB. A ``DROP TABLE IF
EXISTS`` of the scratch tables makes a crash *before* the old tables are dropped
recoverable by re-run.

RECOVERY (SQLite DDL auto-commits and is not rolled back by Alembic on crash, so a
mid-rebuild crash can leave the DB ahead of Alembic):
  1. MANDATORY pre-deploy, services stopped:  cp inventory.db inventory.db.bak
  2. If the migration crashes: stop services, restore inventory.db.bak, and re-run
     ``alembic upgrade head`` (the guard no-ops a completed re-run; a restored backup
     re-runs from a clean old state).
  3. If the rename completed but Alembic was not stamped (DB ahead of Alembic;
     duplicate/already-exists errors on start): manually stamp
     ``sqlite3 inventory.db "UPDATE alembic_version SET version_num = '1a63b54732eb';"``

An orphan ``prices`` row (a price with no matching ``product_variants`` row) would surface
as a ``PRAGMA foreign_key_check`` failure that fails this migration loudly — recoverable
via the backup, not a silent mid-deploy surprise. By design no such row exists.

The downgrade assumes the single-retailer invariant (it collapses back to barcode-only
PKs and would collide on a barcode shared across retailers, which cannot occur this
sprint).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '1a63b54732eb'
down_revision: Union[str, Sequence[str], None] = '605be7ba628c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(conn, table: str, column: str) -> bool:
    rows = conn.execute(sa.text(f"PRAGMA table_info('{table}')")).fetchall()
    return any(r[1] == column for r in rows)


def _count(conn, table: str) -> int:
    return conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def upgrade() -> None:
    conn = op.get_bind()

    # --- Idempotency guard: check BOTH re-keyed tables ---------------------------------
    inv_done = _has_column(conn, 'inventory', 'retailer_id')
    si_done = _has_column(conn, 'session_items', 'retailer_id')
    if inv_done and si_done:
        return  # already applied — clean no-op (e.g. re-run after a manual stamp)
    if inv_done != si_done:
        raise RuntimeError(
            "1b re-key is partially applied: "
            f"inventory.retailer_id={'present' if inv_done else 'absent'}, "
            f"session_items.retailer_id={'present' if si_done else 'absent'}. "
            "This indicates a crash mid-rebuild. Restore inventory.db.bak and re-run "
            "'alembic upgrade head'. Do not attempt to auto-complete."
        )

    op.execute("PRAGMA foreign_keys = OFF")

    # Recover a crash-before-drop by clearing any leftover scratch tables.
    op.execute("DROP TABLE IF EXISTS new_inventory")
    op.execute("DROP TABLE IF EXISTS new_session_items")
    op.execute("DROP TABLE IF EXISTS new_prices")

    # Resolve the backfill retailer by name (seeded in initial_schema.sql). Never hardcode.
    retailer_id = conn.execute(
        sa.text("SELECT id FROM retailers WHERE name = :n"),
        {"n": "Sainsbury's"},
    ).scalar_one()

    # Pre-counts for the invariance assertions below (prices is rebuilt too).
    inv_before = _count(conn, 'inventory')
    si_before = _count(conn, 'session_items')
    prices_before = _count(conn, 'prices')

    # --- Create rebuilt tables ---------------------------------------------------------
    op.create_table(
        'new_inventory',
        sa.Column('barcode', sa.Text(), nullable=False),
        sa.Column('retailer_id', sa.Integer(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('minimum_quantity', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('barcode', 'retailer_id'),
        sa.ForeignKeyConstraint(['barcode'], ['barcodes.barcode']),
        sa.ForeignKeyConstraint(['retailer_id'], ['retailers.id']),
    )

    op.create_table(
        'new_session_items',
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('barcode', sa.Text(), nullable=False),
        sa.Column('retailer_id', sa.Integer(), nullable=False),
        sa.Column('delta', sa.Integer(), nullable=False),
        sa.Column('info_status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('price_status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('first_scanned_at', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('session_id', 'barcode', 'retailer_id'),
        sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['barcode'], ['barcodes.barcode']),
        sa.ForeignKeyConstraint(['retailer_id'], ['retailers.id']),
        sa.CheckConstraint('delta >= 0', name='session_items_delta_nonneg'),
        sa.CheckConstraint(
            "info_status IN ('pending', 'resolved', 'failed')",
            name='session_items_info_status_check',
        ),
        sa.CheckConstraint(
            "price_status IN ('pending', 'resolved', 'failed', 'not_possible')",
            name='session_items_price_status_check',
        ),
    )

    op.create_table(
        'new_prices',
        sa.Column('barcode', sa.Text(), nullable=False),
        sa.Column('retailer_id', sa.Integer(), nullable=False),
        sa.Column('price_pence', sa.Integer()),
        sa.Column('price_type', sa.Text(), nullable=False, server_default='unit'),
        sa.Column('product_url', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('barcode', 'retailer_id'),
        sa.ForeignKeyConstraint(
            ['barcode', 'retailer_id'],
            ['product_variants.barcode', 'product_variants.retailer_id'],
        ),
        sa.CheckConstraint("price_type IN ('unit', 'per_kg')", name='new_prices_price_type_check'),
    )

    # --- Copy data (backfill retailer_id on the two re-keyed tables) -------------------
    op.execute(
        sa.text(
            "INSERT INTO new_inventory (barcode, retailer_id, quantity, minimum_quantity) "
            "SELECT barcode, :rid, quantity, minimum_quantity FROM inventory"
        ).bindparams(rid=retailer_id)
    )
    op.execute(
        sa.text(
            "INSERT INTO new_session_items "
            "(session_id, barcode, retailer_id, delta, info_status, price_status, first_scanned_at) "
            "SELECT session_id, barcode, :rid, delta, info_status, price_status, first_scanned_at "
            "FROM session_items"
        ).bindparams(rid=retailer_id)
    )
    op.execute(
        "INSERT INTO new_prices (barcode, retailer_id, price_pence, price_type, product_url) "
        "SELECT barcode, retailer_id, price_pence, price_type, product_url FROM prices"
    )

    # --- Drop old, rename new into place -----------------------------------------------
    op.drop_table('inventory')
    op.drop_table('session_items')
    op.drop_table('prices')
    op.execute("ALTER TABLE new_inventory RENAME TO inventory")
    op.execute("ALTER TABLE new_session_items RENAME TO session_items")
    op.execute("ALTER TABLE new_prices RENAME TO prices")

    # --- Recreate partial indexes (verbatim WHERE; create_index can't express them) ----
    op.execute(
        "CREATE INDEX idx_session_items_info_pending "
        "ON session_items(first_scanned_at) "
        "WHERE info_status = 'pending'"
    )
    op.execute(
        "CREATE INDEX idx_session_items_price_pending "
        "ON session_items(first_scanned_at) "
        "WHERE price_status = 'pending'"
    )

    # --- Post-conditions ---------------------------------------------------------------
    violations = conn.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
    if violations:
        raise RuntimeError(f"foreign_key_check failed after re-key: {violations}")

    for table, before in (
        ('inventory', inv_before),
        ('session_items', si_before),
        ('prices', prices_before),
    ):
        after = _count(conn, table)
        if after != before:
            raise RuntimeError(f"row count changed for {table}: {before} -> {after}")

    op.execute("PRAGMA foreign_keys = ON")


def downgrade() -> None:
    conn = op.get_bind()
    op.execute("PRAGMA foreign_keys = OFF")

    op.execute("DROP TABLE IF EXISTS old_inventory")
    op.execute("DROP TABLE IF EXISTS old_session_items")
    op.execute("DROP TABLE IF EXISTS old_prices")

    # Restore barcode-only inventory PK.
    op.create_table(
        'old_inventory',
        sa.Column('barcode', sa.Text(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('minimum_quantity', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('barcode'),
        sa.ForeignKeyConstraint(['barcode'], ['barcodes.barcode']),
    )

    # Restore (session_id, barcode) session_items PK; drop retailer_id.
    op.create_table(
        'old_session_items',
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('barcode', sa.Text(), nullable=False),
        sa.Column('delta', sa.Integer(), nullable=False),
        sa.Column('info_status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('price_status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('first_scanned_at', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('session_id', 'barcode'),
        sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['barcode'], ['barcodes.barcode']),
        sa.CheckConstraint('delta >= 0', name='session_items_delta_nonneg'),
        sa.CheckConstraint(
            "info_status IN ('pending', 'resolved', 'failed')",
            name='session_items_info_status_check',
        ),
        sa.CheckConstraint(
            "price_status IN ('pending', 'resolved', 'failed', 'not_possible')",
            name='session_items_price_status_check',
        ),
    )

    # Restore prices' two separate FKs (to barcodes and retailers).
    op.create_table(
        'old_prices',
        sa.Column('barcode', sa.Text(), nullable=False),
        sa.Column('retailer_id', sa.Integer(), nullable=False),
        sa.Column('price_pence', sa.Integer()),
        sa.Column('price_type', sa.Text(), nullable=False, server_default='unit'),
        sa.Column('product_url', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('barcode', 'retailer_id'),
        sa.ForeignKeyConstraint(['barcode'], ['barcodes.barcode']),
        sa.ForeignKeyConstraint(['retailer_id'], ['retailers.id']),
        sa.CheckConstraint("price_type IN ('unit', 'per_kg')", name='old_prices_price_type_check'),
    )

    op.execute(
        "INSERT INTO old_inventory (barcode, quantity, minimum_quantity) "
        "SELECT barcode, quantity, minimum_quantity FROM inventory"
    )
    op.execute(
        "INSERT INTO old_session_items "
        "(session_id, barcode, delta, info_status, price_status, first_scanned_at) "
        "SELECT session_id, barcode, delta, info_status, price_status, first_scanned_at "
        "FROM session_items"
    )
    op.execute(
        "INSERT INTO old_prices (barcode, retailer_id, price_pence, price_type, product_url) "
        "SELECT barcode, retailer_id, price_pence, price_type, product_url FROM prices"
    )

    op.drop_table('inventory')
    op.drop_table('session_items')
    op.drop_table('prices')
    op.execute("ALTER TABLE old_inventory RENAME TO inventory")
    op.execute("ALTER TABLE old_session_items RENAME TO session_items")
    op.execute("ALTER TABLE old_prices RENAME TO prices")

    op.execute(
        "CREATE INDEX idx_session_items_info_pending "
        "ON session_items(first_scanned_at) "
        "WHERE info_status = 'pending'"
    )
    op.execute(
        "CREATE INDEX idx_session_items_price_pending "
        "ON session_items(first_scanned_at) "
        "WHERE price_status = 'pending'"
    )

    violations = conn.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
    if violations:
        raise RuntimeError(f"foreign_key_check failed after downgrade: {violations}")

    op.execute("PRAGMA foreign_keys = ON")
