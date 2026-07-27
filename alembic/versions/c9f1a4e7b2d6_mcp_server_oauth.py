"""mcp_server: add OAUTH auth type + OAuth token columns

Adds ``OAUTH`` to ``mcp_server_auth_type`` and three nullable OAuth columns
(``oauth_client_encrypted`` / ``oauth_tokens_encrypted`` / ``oauth_scope``).
The enum grows via the transaction-safe swap-type idiom (mirrors
``e5b9c1a4d7f3``) rather than ``ALTER TYPE ... ADD VALUE`` (not transaction
safe). The existing NONE/BEARER/HEADER rows cast to themselves.

Revision ID: c9f1a4e7b2d6
Revises: b7e4d9a2c5f1
Create Date: 2026-07-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9f1a4e7b2d6"
down_revision: Union[str, Sequence[str], None] = "b7e4d9a2c5f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ENUM = "mcp_server_auth_type"


def _swap_enum(values: tuple[str, ...]) -> None:
    """Recreate ``mcp_server_auth_type`` with ``values`` and re-cast the column."""
    op.execute(f"ALTER TYPE {_ENUM} RENAME TO {_ENUM}_old")
    op.execute(f"CREATE TYPE {_ENUM} AS ENUM ({', '.join(repr(v) for v in values)})")
    op.execute("ALTER TABLE mcp_server ALTER COLUMN auth_type DROP DEFAULT")
    op.execute(
        f"ALTER TABLE mcp_server ALTER COLUMN auth_type TYPE {_ENUM} "
        f"USING (auth_type::text)::{_ENUM}"
    )
    op.execute(f"ALTER TABLE mcp_server ALTER COLUMN auth_type SET DEFAULT 'NONE'::{_ENUM}")
    op.execute(f"DROP TYPE {_ENUM}_old")


def upgrade() -> None:
    _swap_enum(("NONE", "BEARER", "HEADER", "OAUTH"))
    op.add_column("mcp_server", sa.Column("oauth_client_encrypted", sa.Text(), nullable=True))
    op.add_column("mcp_server", sa.Column("oauth_tokens_encrypted", sa.Text(), nullable=True))
    op.add_column("mcp_server", sa.Column("oauth_scope", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_server", "oauth_scope")
    op.drop_column("mcp_server", "oauth_tokens_encrypted")
    op.drop_column("mcp_server", "oauth_client_encrypted")
    # Assumes no OAUTH rows remain — the cast to the 3-value enum fails otherwise
    # (delete OAUTH connectors before downgrading).
    _swap_enum(("NONE", "BEARER", "HEADER"))
