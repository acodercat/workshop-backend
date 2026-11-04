"""project.icon: the icon the client draws for a project

Named by the client's catalog, NULL until the owner picks one. Nullable
with no default: a catalog-only change, no rewrite of ``project``.

Revision ID: b8e2d4f6a3c1
Revises: f2c7b9e4a1d6
Create Date: 2026-11-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8e2d4f6a3c1"
down_revision: Union[str, Sequence[str], None] = "f2c7b9e4a1d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("project", sa.Column("icon", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("project", "icon")
