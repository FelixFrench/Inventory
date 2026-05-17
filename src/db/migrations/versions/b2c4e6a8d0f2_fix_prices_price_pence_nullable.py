"""fix_prices_price_pence_nullable

Revision ID: b2c4e6a8d0f2
Revises: 0a3435ca1dec
Create Date: 2026-05-17

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b2c4e6a8d0f2'
down_revision: Union[str, Sequence[str], None] = '0a3435ca1dec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.execute("""
        CREATE TABLE prices_new (
            product_variant_id INTEGER NOT NULL REFERENCES product_variants(id),
            retailer_id        INTEGER NOT NULL REFERENCES retailers(id),
            price_pence        INTEGER,
            price_type         TEXT NOT NULL DEFAULT 'unit'
                                   CHECK(price_type IN ('unit', 'per_kg')),
            last_updated       TIMESTAMP,
            PRIMARY KEY (product_variant_id, retailer_id)
        )
    """)
    op.execute("INSERT INTO prices_new SELECT * FROM prices")
    op.execute("DROP TABLE prices")
    op.execute("ALTER TABLE prices_new RENAME TO prices")


def downgrade():
    op.execute("""
        CREATE TABLE prices_old (
            product_variant_id INTEGER NOT NULL REFERENCES product_variants(id),
            retailer_id        INTEGER NOT NULL REFERENCES retailers(id),
            price_pence        INTEGER NOT NULL,
            price_type         TEXT NOT NULL DEFAULT 'unit'
                                   CHECK(price_type IN ('unit', 'per_kg')),
            last_updated       TIMESTAMP,
            PRIMARY KEY (product_variant_id, retailer_id)
        )
    """)
    op.execute("INSERT INTO prices_old SELECT * FROM prices")
    op.execute("DROP TABLE prices")
    op.execute("ALTER TABLE prices_old RENAME TO prices")
