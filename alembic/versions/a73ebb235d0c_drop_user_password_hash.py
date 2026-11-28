"""drop user.password_hash

Customer credentials belong to the identity provider: nothing sets, reads or
verifies a local customer password. The column held only the bcrypt hashes
of accounts that predate federation, so it goes.

**Downgrade** re-adds the column nullable and empty; the dropped hashes are
not restored.

Revision ID: a73ebb235d0c
Revises: 04bfcad62f61
Create Date: 2026-11-28

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "a73ebb235d0c"
down_revision: Union[str, Sequence[str], None] = "04bfcad62f61"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("user", "password_hash")


def downgrade() -> None:
    op.add_column("user", sa.Column("password_hash", sa.String(length=255), nullable=True))
