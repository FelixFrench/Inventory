"""initial_schema

Revision ID: 0a3435ca1dec
Revises: 
Create Date: 2026-05-16 18:24:08.045461

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0a3435ca1dec'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    from pathlib import Path
    schema = (Path(__file__).parent.parent.parent / "schema.sql").read_text()
    
    # Execute each statement separately — op.execute() only accepts one at a time
    statements = [s.strip() for s in schema.split(";") if s.strip()]
    for statement in statements:
        op.execute(statement)
    
    op.execute("""
        CREATE TABLE config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    
    op.execute("INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider')")
    op.execute("INSERT INTO config (key, value) VALUES ('scan_mode', 'out')")


def downgrade():
    op.execute("DROP TABLE IF EXISTS pending_lookups")
    op.execute("DROP TABLE IF EXISTS scan_events")
    op.execute("DROP TABLE IF EXISTS config")
    op.execute("DROP TABLE IF EXISTS inventory")
    op.execute("DROP TABLE IF EXISTS prices")
    op.execute("DROP TABLE IF EXISTS barcodes")
    op.execute("DROP TABLE IF EXISTS product_variants")
    op.execute("DROP TABLE IF EXISTS retailers")

