"""mcp_server.resources_enabled

Whether the agent may browse and read what this Connector holds at a URI.

MCP has two halves and the Connectors page only ever governed one. ``connected``
decides whether a Connector is used at all, and ``disabled_tools`` switches off
individual *tools* — matched against the bare names the server publishes. The
resources half is reached through three tools the host synthesizes under a
prefix deliberately outside that namespace, so no entry in ``disabled_tools``
could ever name one. Connecting a Connector therefore granted read access to
everything it holds, and nothing on the page said so or could stop it.

**Existing rows land on FALSE, which is the point of the column.** The worker
already builds these tools for every resources-capable server, so defaulting to
TRUE would preserve today's behaviour — and today's behaviour is what this
fixes. An upgrade must not leave anyone's data more exposed than the release
before it did.

**Deploy note: apply before deploying either the backend or the worker.** The
API selects the whole row, and the worker names this column in its per-turn
Connector resolve — where an old schema does not fail loudly but resolves *zero*
Connectors, costing every user every MCP tool on every turn with no notice. And
do not downgrade a live environment: dropping the column under a running worker
is the same outage.

Revision ID: b6e3d9a70f52
Revises: f2c8b04e5da7
Create Date: 2026-08-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b6e3d9a70f52"
down_revision: Union[str, Sequence[str], None] = "f2c8b04e5da7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcp_server",
        sa.Column(
            "resources_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("mcp_server", "resources_enabled")
