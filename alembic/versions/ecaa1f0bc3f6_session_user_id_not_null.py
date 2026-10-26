"""session.user_id NOT NULL

Nullable "for anonymous sessions", a path that never shipped: the one
INSERT site (``build_session``) always sets it, and the FK is
``ON DELETE CASCADE``, so nothing can null it afterwards. Every consumer
paid for the case anyway — outer joins, null-owner branches, optional
owner fields.

Refuses to run, naming the count, if a session has no owner: assign or
delete those first.

Revision ID: ecaa1f0bc3f6
Revises: 89b31feadb19
Create Date: 2026-10-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "ecaa1f0bc3f6"
down_revision: Union[str, Sequence[str], None] = "89b31feadb19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    ownerless = op.get_bind().scalar(sa.text("SELECT count(*) FROM session WHERE user_id IS NULL"))
    if ownerless:
        raise RuntimeError(
            f"{ownerless} session row(s) have no user_id; assign or delete them first"
        )
    op.alter_column("session", "user_id", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    op.alter_column("session", "user_id", existing_type=sa.Integer(), nullable=True)
