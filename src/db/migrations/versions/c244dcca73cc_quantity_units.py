"""quantity_units

Revision ID: c244dcca73cc
Revises: a1b2c3d4e5f6
Create Date: 2026-06-20

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = 'c244dcca73cc'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(product_variants)")).fetchall()}

    if "product_quantity" not in cols:
        op.execute(text("ALTER TABLE product_variants ADD COLUMN product_quantity TEXT"))
        op.execute(text(
            "UPDATE product_variants SET product_quantity = printf('%g', weight_g) || 'g'"
            " WHERE weight_g IS NOT NULL AND product_quantity IS NULL"
        ))


def downgrade() -> None:
    pass  # Not supported: TEXT column cannot be reversed to INTEGER without data loss
