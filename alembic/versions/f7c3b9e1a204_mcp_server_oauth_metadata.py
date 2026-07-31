"""mcp_server.oauth_metadata

The authorization server's own metadata and the RFC 9728 resource document,
captured when the user authorized.

Whoever renews a grant reads this to know where to send the rotation and what
its ``resource`` indicator must name — the backend's token refresher, and the
worker, which renews on demand during a turn. Without it there is nothing to guess
from safely: posting the refresh token to ``urljoin(<MCP origin>, "/token")``
is right only for a server that hosts its own token endpoint, and would
otherwise hand the grant — and the client secret under it — to the resource
server. A row missing it is skipped rather than guessed at.

Rediscovering both on every refresh would be correct too, but it puts a
three-request sequence and its own timeout in front of every rotation — for
documents that were fetched during authorization and essentially never change.
Caching the answer where the grant lives removes all of that. RFC 8414's caution
against caching metadata indefinitely is aimed at ``jwks_uri`` rotation, which
concerns the party *validating* tokens; a client only needs the token endpoint.
A provider that does move one makes the refresh fail, the Connector asks to be
reauthorized, and that flow rewrites this column — so it heals itself.

Not encrypted, unlike its ``oauth_*`` siblings: these are public discovery
documents, and sealing them would imply a sensitivity they do not have.

NULL for rows authorized before this column existed. Both readers treat that as
"cannot rotate safely" rather than guessing: the backend leaves the Connector
alone, and the worker asks the user to authorize again, since a turn that cannot
renew has nothing to fall back on. Either way such a row recovers on its next
authorization.

**Deploy note: apply before deploying either service** — both address a rotation
from this column.

Revision ID: f7c3b9e1a204
Revises: e4a9c2b7d503
Create Date: 2026-07-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "f7c3b9e1a204"
down_revision: Union[str, Sequence[str], None] = "e4a9c2b7d503"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcp_server",
        sa.Column(
            "oauth_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("mcp_server", "oauth_metadata")
