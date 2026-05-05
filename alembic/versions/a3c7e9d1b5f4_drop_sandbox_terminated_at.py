"""drop sandbox.terminated_at

Column was declared in the initial schema as a "reaper writes the
kill time here" slot, but the reaper never wired it up and no other
code reads or writes the value. ``sandbox.status = TERMINATED``
already conveys "this container is dead"; the timestamp half of that
contract was never realised.

Revision ID: a3c7e9d1b5f4
Revises: f1c4a9e2b8d6
Create Date: 2026-05-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3c7e9d1b5f4"
down_revision: Union[str, Sequence[str], None] = "f1c4a9e2b8d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("sandbox", "terminated_at")


def downgrade() -> None:
    op.add_column(
        "sandbox",
        sa.Column("terminated_at", sa.TIMESTAMP(), nullable=True),
    )
