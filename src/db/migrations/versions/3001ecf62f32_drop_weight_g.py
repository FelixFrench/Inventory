"""drop_weight_g

Revision ID: 3001ecf62f32
Revises: c244dcca73cc
Create Date: 2026-06-20

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = '3001ecf62f32'
down_revision: Union[str, Sequence[str], None] = 'c244dcca73cc'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(product_variants)")).fetchall()}
    if "weight_g" in cols:
        op.execute(text("ALTER TABLE product_variants DROP COLUMN weight_g"))


def downgrade() -> None:
    pass  # Not supported
