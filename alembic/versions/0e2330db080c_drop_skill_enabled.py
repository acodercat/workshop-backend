"""drop skill.enabled

A transitional mirror since ``skill.status`` replaced it: written at the
value a pre-``status`` worker's ``COALESCE(p.enabled, s.enabled)``
expected, read by nothing in this backend. The shared ``core/agent-worker``
has filtered on ``status`` since, so the column goes.

**Deploy order.** Every deployment's worker must already be past the
``status`` change; one still reading ``s.enabled`` fails its skill
lookups once this runs.

Revision ID: 0e2330db080c
Revises: c86e19190d97
Create Date: 2026-10-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0e2330db080c"
down_revision: Union[str, Sequence[str], None] = "c86e19190d97"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("skill", "enabled")


def downgrade() -> None:
    op.add_column(
        "skill",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
