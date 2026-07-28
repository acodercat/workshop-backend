"""mcp_server.disabled_tools

Per-tool switches inside a Connector. Holds the **bare server-side tool names**
the user has switched off (``["delete_page"]``); everything else the server
exposes stays available. Storing the exceptions rather than the selection is
what makes it survive a changing tool list: a server that adds a tool has it
available immediately, and a server that drops one leaves an inert stale entry
instead of a hole in an allowlist.

Bare names, never axon's ``mcp__{connector}__{tool}`` form — the prefix derives
from the Connector's editable name and is sanitized and hash-truncated, so it is
lossy and could not be matched back.

Deliberately *not* folded into ``tools_preview``: that column is overwritten
wholesale on every re-probe, which would silently erase the switches.

NOT NULL with ``'[]'::jsonb`` (the ``session_turn.mcp_servers`` pattern, not
``tools_preview``'s nullable one) so there is no tri-state NULL-vs-empty to
handle — existing rows read as "nothing switched off", which is the pre-feature
behaviour.

**Deploy note: apply this BEFORE deploying the worker.** The worker reads this
column in its per-turn Connector resolve, so a worker on the old schema resolves
*zero* Connectors and every user loses every MCP tool on every turn.

Revision ID: a7d2f4e9c613
Revises: f1a6d3c8b507
Create Date: 2026-07-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "a7d2f4e9c613"
down_revision: Union[str, Sequence[str], None] = "f1a6d3c8b507"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcp_server",
        sa.Column(
            "disabled_tools",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("mcp_server", "disabled_tools")
