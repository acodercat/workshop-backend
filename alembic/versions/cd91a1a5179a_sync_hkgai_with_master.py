"""sync hkgai with master

Revision ID: cd91a1a5179a
Revises: a1c7f3e9d2b6, f2c8b04e5da7
Create Date: 2026-08-08 19:23:00.424704

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cd91a1a5179a'
down_revision: Union[str, Sequence[str], None] = ('a1c7f3e9d2b6', 'f2c8b04e5da7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
