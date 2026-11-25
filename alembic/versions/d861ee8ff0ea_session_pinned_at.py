"""session.pinned_at: when the owner pinned a session

Pinned sessions list first, the most recently pinned on top. Nullable
with no default: NULL is "not pinned", so no row of ``session`` is
rewritten.

Revision ID: d861ee8ff0ea
Revises: c7d2e9a4b1f3
Create Date: 2026-11-17

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d861ee8ff0ea"
down_revision: Union[str, Sequence[str], None] = "c7d2e9a4b1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("session", sa.Column("pinned_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("session", "pinned_at")
