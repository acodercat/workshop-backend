"""add user.avatar_revision

A monotonically-increasing variant counter for the user's avatar.
``0`` is the default look (deterministic from id); each "shuffle"
button click increments by 1, producing a different look. The
frontend joins ``user.id`` + this counter into the seed string fed
to ``boring-avatars``' ``name`` prop, so any revision >= 0 maps to
a stable visual.

Chosen over a free-form ``avatar_seed: str`` column because:

- Bounded width (4 bytes vs ``String(128)``) — can't trigger
  ``string_too_long`` validation no matter how many times the user
  shuffles.
- Single source of truth for "which variant is the user on" without
  leaking the rendering library's seed format into the schema.
- Future-proof: swapping ``boring-avatars`` for any other
  deterministic avatar lib (DiceBear / identicon / in-house) leaves
  the column meaning intact — only the frontend's seed-derivation
  function changes.

Revision ID: 470bec234c01
Revises: 12dc6d69a9ad
Create Date: 2026-05-07 17:46:12.024137

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '470bec234c01'
down_revision: Union[str, Sequence[str], None] = '12dc6d69a9ad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user',
        sa.Column(
            'avatar_revision',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
    )


def downgrade() -> None:
    op.drop_column('user', 'avatar_revision')
