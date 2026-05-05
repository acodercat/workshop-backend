"""drop sandbox.user_id

The column has never been populated by any code path — neither the
worker's ``record_sandbox_use`` nor any backend handler writes to it.
Sandbox ownership is implicit through ``session.user_id``; modelling
it twice was a leftover that made the table confusingly look like a
user-owned resource.

Revision ID: fd72eadcb874
Revises: 9c4a8b1e7d62
Create Date: 2026-04-24 14:48:36.900350

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "fd72eadcb874"
down_revision: Union[str, Sequence[str], None] = "9c4a8b1e7d62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("sandbox_user_id_fkey", "sandbox", type_="foreignkey")
    op.drop_column("sandbox", "user_id")


def downgrade() -> None:
    op.add_column(
        "sandbox",
        sa.Column("user_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "sandbox_user_id_fkey",
        "sandbox",
        "user",
        ["user_id"],
        ["id"],
    )
