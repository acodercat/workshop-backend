"""mcp_server.oauth_reauthorization_required

Records that a Connector's stored OAuth grant stopped working and only a
fresh interactive authorization can revive it — the user
revoked it at the provider, the refresh token expired, or a rotated refresh
token was rejected. Deliberately *not* folded into ``connected``: that column
is the user's own switch, and conflating "I turned this off" with "the provider
cut us off" would both lose the distinction and drop the Connector off the
Connectors page, where the badge needs to be shown. Carries the ``oauth_``
prefix of its siblings because only the OAuth path can ever set it.

Existing rows default to ``false`` — the flag only ever becomes true after an
authorization server answers a refresh with ``invalid_grant``, so backfilling is
meaningless.

The worker writes it, on a renewal the authorization server refused, and reads
it to avoid spending another request on a grant already judged dead. The backend
only clears it, and only where a fresh interactive authorization has actually
replaced the grant.

The worker also tells the user about it, which is the point of writing it there:
a flagged Connector gets a system reminder naming it, so the model can send them
to the Connectors page rather than retrying a tool that cannot work. Its tools
are dropped from that turn's catalog only if its access token has *also* expired;
a flag says the refresh token is dead, and the access token under it can still
have an hour left.

**Deploy note: apply this BEFORE deploying either service.** The worker reads
and writes it on every OAuth Connector it connects, and the backend's authorize
flow clears it; on an old schema both raise.

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
