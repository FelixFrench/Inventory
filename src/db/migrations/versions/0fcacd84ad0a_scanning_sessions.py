"""scanning_sessions

Revision ID: 0fcacd84ad0a
Revises: 0a3435ca1dec
Create Date: 2026-05-27 22:55:20.336171

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0fcacd84ad0a'
down_revision: Union[str, Sequence[str], None] = '0a3435ca1dec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys = OFF")

    # Drop transient / unreferenced tables
    op.drop_table('pending_lookups')
    op.drop_table('canonical_products')

    # Create Phase 1 replacement tables before migrating data
    op.create_table(
        'new_barcodes',
        sa.Column('barcode', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('barcode'),
    )

    op.create_table(
        'new_product_variants',
        sa.Column('barcode', sa.Text(), nullable=False),
        sa.Column('retailer_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.Text()),
        sa.Column('brand', sa.Text()),
        sa.Column('weight_g', sa.Float()),
        sa.PrimaryKeyConstraint('barcode', 'retailer_id'),
        sa.ForeignKeyConstraint(['barcode'], ['new_barcodes.barcode']),
        sa.ForeignKeyConstraint(['retailer_id'], ['retailers.id']),
    )

    op.create_table(
        'new_prices',
        sa.Column('barcode', sa.Text(), nullable=False),
        sa.Column('retailer_id', sa.Integer(), nullable=False),
        sa.Column('price_pence', sa.Integer()),
        sa.Column('price_type', sa.Text(), nullable=False, server_default='unit'),
        sa.PrimaryKeyConstraint('barcode', 'retailer_id'),
        sa.ForeignKeyConstraint(['barcode'], ['new_barcodes.barcode']),
        sa.CheckConstraint("price_type IN ('unit', 'per_kg')", name='new_prices_price_type_check'),
    )

    op.create_table(
        'new_inventory',
        sa.Column('barcode', sa.Text(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('minimum_quantity', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('barcode'),
        sa.ForeignKeyConstraint(['barcode'], ['new_barcodes.barcode']),
    )

    # Migrate live data while old tables still exist
    op.execute(
        "INSERT INTO new_barcodes (barcode) "
        "SELECT DISTINCT barcode FROM barcodes"
    )
    op.execute(
        "INSERT INTO new_product_variants (barcode, retailer_id, name, brand, weight_g) "
        "SELECT b.barcode, b.retailer_id, pv.name, pv.brand, pv.weight_g "
        "FROM product_variants pv "
        "JOIN barcodes b ON b.product_variant_id = pv.id"
    )
    op.execute(
        "INSERT INTO new_prices (barcode, retailer_id, price_pence, price_type) "
        "SELECT b.barcode, p.retailer_id, p.price_pence, p.price_type "
        "FROM prices p "
        "JOIN product_variants pv ON pv.id = p.product_variant_id "
        "JOIN barcodes b ON b.product_variant_id = pv.id"
    )
    op.execute(
        "INSERT INTO new_inventory (barcode, quantity, minimum_quantity) "
        "SELECT b.barcode, i.quantity, i.minimum_quantity "
        "FROM inventory i "
        "JOIN product_variants pv ON pv.id = i.product_variant_id "
        "JOIN barcodes b ON b.product_variant_id = pv.id"
    )

    # Drop old tables
    op.drop_table('inventory')
    op.drop_table('prices')
    op.drop_table('product_variants')
    op.drop_table('barcodes')

    # Rename new tables into place
    op.execute("ALTER TABLE new_inventory RENAME TO inventory")
    op.execute("ALTER TABLE new_prices RENAME TO prices")
    op.execute("ALTER TABLE new_product_variants RENAME TO product_variants")
    op.execute("ALTER TABLE new_barcodes RENAME TO barcodes")

    # Sessions table
    op.create_table(
        'sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('type', sa.Text(), nullable=False),
        sa.Column('started_at', sa.Text(), nullable=False),
        sa.Column('recovered_at', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("type IN ('in', 'out')", name='sessions_type_check'),
    )

    # Session items table
    op.create_table(
        'session_items',
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
            name='session_items_info_status_check'
        ),
        sa.CheckConstraint(
            "price_status IN ('pending', 'resolved', 'failed', 'not_possible')",
            name='session_items_price_status_check'
        ),
    )

    # Partial indexes — Alembic create_index does not support WHERE clauses
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

    # Worker state singleton — epoch sentinel ensures first OFF call satisfies the 4s gap
    op.create_table(
        'worker_state',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('off_last_called_at', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint('id = 1', name='worker_state_singleton'),
    )
    op.execute("INSERT INTO worker_state (id, off_last_called_at) VALUES (1, '1970-01-01T00:00:00')")

    op.execute("DELETE FROM config WHERE key = 'scan_mode'")

    op.execute("PRAGMA foreign_keys = ON")


def downgrade() -> None:
    op.execute("PRAGMA foreign_keys = OFF")

    op.execute("INSERT INTO config (key, value) VALUES ('scan_mode', 'out')")

    op.drop_table('worker_state')
    op.execute("DROP INDEX IF EXISTS idx_session_items_price_pending")
    op.execute("DROP INDEX IF EXISTS idx_session_items_info_pending")
    op.drop_table('session_items')
    op.drop_table('sessions')

    # Restore V1.0 table structures with data migration
    op.create_table(
        'canonical_products',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'old_product_variants',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('canonical_product_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.Text()),
        sa.Column('brand', sa.Text()),
        sa.Column('weight_g', sa.Float()),
        sa.Column('info_source', sa.Text()),
        sa.Column('info_last_updated', sa.Text()),
        sa.PrimaryKeyConstraint('id'),
    )

    op.execute(
        "INSERT INTO old_product_variants (name, brand, weight_g) "
        "SELECT name, brand, weight_g FROM product_variants"
    )

    op.create_table(
        'old_barcodes',
        sa.Column('barcode', sa.Text(), nullable=False),
        sa.Column('retailer_id', sa.Integer(), nullable=False),
        sa.Column('product_variant_id', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('barcode', 'retailer_id'),
    )

    op.execute(
        "INSERT INTO old_barcodes (barcode, retailer_id, product_variant_id) "
        "SELECT pv2.barcode, pv2.retailer_id, opv.id "
        "FROM product_variants pv2 "
        "JOIN old_product_variants opv "
        "    ON opv.name IS pv2.name "
        "    AND opv.brand IS pv2.brand "
        "    AND opv.weight_g IS pv2.weight_g"
    )

    op.execute(
        "INSERT OR IGNORE INTO old_barcodes (barcode, retailer_id, product_variant_id) "
        "SELECT b.barcode, 1, NULL "
        "FROM barcodes b "
        "WHERE NOT EXISTS (SELECT 1 FROM old_barcodes ob WHERE ob.barcode = b.barcode)"
    )

    op.create_table(
        'old_prices',
        sa.Column('product_variant_id', sa.Integer(), nullable=False),
        sa.Column('retailer_id', sa.Integer(), nullable=False),
        sa.Column('price_pence', sa.Integer()),
        sa.Column('price_type', sa.Text(), nullable=False, server_default='unit'),
        sa.Column('last_updated', sa.Text()),
        sa.PrimaryKeyConstraint('product_variant_id', 'retailer_id'),
        sa.CheckConstraint("price_type IN ('unit', 'per_kg')", name='old_prices_price_type_check'),
    )

    op.execute(
        "INSERT INTO old_prices (product_variant_id, retailer_id, price_pence, price_type) "
        "SELECT ob.product_variant_id, p.retailer_id, p.price_pence, p.price_type "
        "FROM prices p "
        "JOIN old_barcodes ob ON ob.barcode = p.barcode AND ob.retailer_id = p.retailer_id "
        "WHERE ob.product_variant_id IS NOT NULL"
    )

    op.create_table(
        'old_inventory',
        sa.Column('product_variant_id', sa.Integer(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('minimum_quantity', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('product_variant_id'),
    )

    op.execute(
        "INSERT INTO old_inventory (product_variant_id, quantity, minimum_quantity) "
        "SELECT ob.product_variant_id, i.quantity, i.minimum_quantity "
        "FROM inventory i "
        "JOIN old_barcodes ob ON ob.barcode = i.barcode "
        "WHERE ob.product_variant_id IS NOT NULL"
    )

    op.drop_table('inventory')
    op.drop_table('prices')
    op.drop_table('product_variants')
    op.drop_table('barcodes')

    op.execute("ALTER TABLE old_inventory RENAME TO inventory")
    op.execute("ALTER TABLE old_prices RENAME TO prices")
    op.execute("ALTER TABLE old_barcodes RENAME TO barcodes")
    op.execute("ALTER TABLE old_product_variants RENAME TO product_variants")

    op.create_table(
        'pending_lookups',
        sa.Column('barcode', sa.Text(), nullable=False),
        sa.Column('retailer_id', sa.Integer(), nullable=False),
        sa.Column('queued_at', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
        sa.PrimaryKeyConstraint('barcode', 'retailer_id'),
        sa.CheckConstraint(
            "status IN ('pending', 'failed', 'done')",
            name='pending_lookups_status_check'
        ),
    )

    op.execute("PRAGMA foreign_keys = ON")
