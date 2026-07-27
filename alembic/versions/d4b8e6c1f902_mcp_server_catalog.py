"""mcp_server_catalog: platform directory of official Connectors

Adds the operator-managed template table plus ``mcp_server.catalog_id``, the
back-pointer set when a user instantiates one of those templates.

``ON DELETE SET NULL`` on the FK is deliberate: retiring a catalog entry demotes
the Connectors already added from it to plain custom ones **that keep working**,
because url / auth_type / credentials live on the user's own row, not on the
template.

Revision ID: d4b8e6c1f902
Revises: c9f1a4e7b2d6
Create Date: 2026-07-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d4b8e6c1f902"
down_revision: Union[str, Sequence[str], None] = "c9f1a4e7b2d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The enum already exists (created with ``mcp_server``); reference it
    # without trying to create it again.
    mcp_server_auth_type = postgresql.ENUM(
        "NONE", "BEARER", "HEADER", "OAUTH",
        name="mcp_server_auth_type", create_type=False,
    )

    op.create_table(
        "mcp_server_catalog",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column(
            "auth_type", mcp_server_auth_type, nullable=False,
            server_default=sa.text("'NONE'"),
        ),
        sa.Column("oauth_scope", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon_url", sa.String(), nullable=True),
        sa.Column(
            "published", sa.Boolean(), nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_mcp_server_catalog_key"),
    )
    # The directory is always read published-first in ``sort_order`` order.
    op.create_index(
        "ix_mcp_server_catalog_published_sort_order",
        "mcp_server_catalog",
        ["published", "sort_order"],
    )

    op.add_column(
        "mcp_server", sa.Column("catalog_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_mcp_server_catalog_id",
        "mcp_server",
        "mcp_server_catalog",
        ["catalog_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_mcp_server_catalog_id", "mcp_server", type_="foreignkey")
    op.drop_column("mcp_server", "catalog_id")
    op.drop_index(
        "ix_mcp_server_catalog_published_sort_order",
        table_name="mcp_server_catalog",
    )
    op.drop_table("mcp_server_catalog")
