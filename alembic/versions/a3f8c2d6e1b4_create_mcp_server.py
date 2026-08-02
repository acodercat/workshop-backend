"""mcp_server: per-user Connector (remote MCP server) config

Backs the "add your own Connector" feature. Each row is one remote MCP
server the user connected: its ``url``, how to authenticate (``auth_type``
+ Fernet-encrypted ``encrypted_credential``), and a cached tool listing
for display. v1 is user-owned only (``user_id`` NOT NULL); the row's own
``enabled`` flag is the on/off switch.

Revision ID: a3f8c2d6e1b4
Revises: e1c5a9d7b3f4
Create Date: 2026-07-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a3f8c2d6e1b4"
down_revision: Union[str, Sequence[str], None] = "e1c5a9d7b3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    mcp_server_auth_type = postgresql.ENUM(
        "NONE", "BEARER", "HEADER", name="mcp_server_auth_type", create_type=False,
    )
    mcp_server_auth_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "mcp_server",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column(
            "auth_type", mcp_server_auth_type, nullable=False,
            server_default=sa.text("'NONE'"),
        ),
        sa.Column("encrypted_credential", sa.Text(), nullable=True),
        sa.Column(
            "tool_count", sa.Integer(), nullable=False, server_default=sa.text("0"),
        ),
        sa.Column(
            "tools_preview",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_mcp_server_user_id_name"),
    )


def downgrade() -> None:
    op.drop_table("mcp_server")
    op.execute("DROP TYPE IF EXISTS mcp_server_auth_type")
