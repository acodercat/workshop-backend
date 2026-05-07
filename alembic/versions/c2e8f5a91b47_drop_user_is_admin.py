"""drop user.is_admin

The flag was never read by any code path — admin endpoints were
removed earlier (see comment at top of :mod:`app.api.endpoints.user`)
and never reintroduced. Carrying a perpetually-false boolean adds
nothing to the schema. If a future admin role is needed, it'll
likely want a richer model anyway (multi-role, scopes, audit).

Revision ID: c2e8f5a91b47
Revises: 8a4f2e1c9b03
Create Date: 2026-05-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c2e8f5a91b47"
down_revision: Union[str, Sequence[str], None] = "8a4f2e1c9b03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("user", "is_admin")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "user",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Drop the server_default we added just to satisfy NOT NULL on
    # backfill — the model never carried one.
    op.alter_column("user", "is_admin", server_default=None)
