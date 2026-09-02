"""mcp_server.description

The server's own ``serverInfo.description`` (MCP): "a human-readable description
of what this implementation does". Captured at probe time so the Connectors page
can show a custom Connector's description — until now only a catalog entry
carried one, so a Connector the user defined themselves showed none.

Nullable and display-only: a server that advertises no description leaves it
NULL, and the UI falls back to a curated catalog description when there is one.
Populated on the next probe (create / re-list / auth change); existing rows read
NULL until then, which the page already handles.

Revision ID: a1f4c7e02b31
Revises: b6e3d9a70f52
Create Date: 2026-09-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a1f4c7e02b31"
down_revision: Union[str, Sequence[str], None] = "b6e3d9a70f52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcp_server",
        sa.Column("description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mcp_server", "description")
