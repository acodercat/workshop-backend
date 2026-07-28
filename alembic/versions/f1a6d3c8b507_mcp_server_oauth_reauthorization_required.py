"""mcp_server.oauth_reauthorization_required

Records that a Connector's stored OAuth grant stopped working and only a
fresh interactive authorization can revive it — the user
revoked it at the provider, the refresh token expired, or a rotated refresh
token was rejected. Deliberately *not* folded into ``connected``: that column
is the user's own switch, and conflating "I turned this off" with "the provider
cut us off" would both lose the distinction and drop the Connector off the
Connectors page, where the badge needs to be shown. Carries the ``oauth_``
prefix of its siblings because only the OAuth path can ever set it.

Existing rows default to ``false`` — the flag only ever becomes true after the
worker observes a failed re-authorization, so backfilling is meaningless.

**Deploy note: apply this BEFORE deploying the worker.** The worker both reads
this column in its per-turn Connector resolve and writes it, so a worker on the
old schema resolves *zero* Connectors — every user loses every MCP tool on
every turn until the migration lands, not the per-Connector degradation the
worker's other failure paths give you.

Revision ID: f1a6d3c8b507
Revises: e2c7a3f5b184
Create Date: 2026-07-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f1a6d3c8b507"
down_revision: Union[str, Sequence[str], None] = "e2c7a3f5b184"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcp_server",
        sa.Column(
            "oauth_reauthorization_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("mcp_server", "oauth_reauthorization_required")
