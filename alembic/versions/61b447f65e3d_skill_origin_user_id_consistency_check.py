"""skill origin/user_id consistency check

Revision ID: 61b447f65e3d
Revises: f866d49dbb32
Create Date: 2026-05-27 21:52:48.101156

Enforces that ``origin`` and ``user_id`` agree on every ``skill`` row:
BUILTIN ↔ user_id IS NULL, CUSTOM ↔ user_id IS NOT NULL. Without this
guard, a row like ``origin='BUILTIN' AND user_id=42`` (e.g. introduced
by manual SQL) would be visible to every user via the worker's
``(user_id = $1 OR origin = 'BUILTIN')`` filter — turning one
customer's CUSTOM bundle into a platform default.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '61b447f65e3d'
down_revision: Union[str, Sequence[str], None] = 'f866d49dbb32'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CONSTRAINT_NAME = "ck_skill_origin_user_id_consistent"


def upgrade() -> None:
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        "skill",
        "(origin = 'BUILTIN' AND user_id IS NULL) "
        "OR (origin = 'CUSTOM' AND user_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "skill", type_="check")
