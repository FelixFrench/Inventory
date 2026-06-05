"""add_product_url_to_prices

Revision ID: a1b2c3d4e5f6
Revises: 0fcacd84ad0a
Create Date: 2026-06-05

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '0fcacd84ad0a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE prices ADD COLUMN product_url TEXT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE prices DROP COLUMN product_url")
