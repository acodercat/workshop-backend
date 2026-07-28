"""mcp_server.enabled -> connected

Renames the per-user activation flag to match the product vocabulary: a
Connector is something you *connect*, not something you enable. The column keeps
its type, NOT NULL and default — only the name changes.

**Deploy note:** the worker reads this column (``get_connected_by_ids``), so the
backend and worker must ship together; a worker on the old name stops resolving
Connectors the moment this runs.

Revision ID: e2c7a3f5b184
Revises: d4b8e6c1f902
Create Date: 2026-07-27
"""
from typing import Sequence, Union

from alembic import op


revision: str = "e2c7a3f5b184"
down_revision: Union[str, Sequence[str], None] = "d4b8e6c1f902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("mcp_server", "enabled", new_column_name="connected")


def downgrade() -> None:
    op.alter_column("mcp_server", "connected", new_column_name="enabled")
