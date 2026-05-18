"""initial_schema

Revision ID: 0a3435ca1dec
Revises: 
Create Date: 2026-05-16 18:24:08.045461

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0a3435ca1dec'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    from pathlib import Path
    schema = (Path(__file__).parent.parent.parent / "schema.sql").read_text()

    # Strip line comments before splitting on ; — some comments contain semicolons
    # which would cause the naive split to produce malformed statements.
    lines = [line for line in schema.splitlines() if not line.strip().startswith("--")]
    schema_no_comments = "\n".join(lines)

    # Execute each statement separately — op.execute() only accepts one at a time
    statements = [s.strip() for s in schema_no_comments.split(";") if s.strip()]
    for statement in statements:
        op.execute(statement)


def downgrade():
    op.execute("DROP TABLE IF EXISTS pending_lookups")
    op.execute("DROP TABLE IF EXISTS scan_events")
    op.execute("DROP TABLE IF EXISTS config")
    op.execute("DROP TABLE IF EXISTS inventory")
    op.execute("DROP TABLE IF EXISTS prices")
    op.execute("DROP TABLE IF EXISTS barcodes")
    op.execute("DROP TABLE IF EXISTS product_variants")
    op.execute("DROP TABLE IF EXISTS retailers")

