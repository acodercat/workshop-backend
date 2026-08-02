"""mcp_server.oauth_refresh_after

The earliest moment this Connector's grant may be renewed again.

Two things at once, and the first is what makes the second safe. It is a
**claim**: a renewal writes a near-future timestamp here before spending the
refresh token, so a peer that locks the row mid-request sees the window and
adopts the result instead of presenting the same single-use token. After the
attempt it becomes a **backoff** — a flat one if the provider was unwell, NULL on
success. Both answer "do not attempt before", which is why one column carries
them.

A column rather than something inferred from ``oauth_tokens_expires_at``, and
rather than in-process state. Inference cannot express a claim at all, and the
expiry alone is the wrong signal besides: MCP's authorization spec asks
authorization servers to prefer short-lived access tokens, so a token that is
already inside any look-ahead the moment it is issued is ordinary rather than
hostile. In-process state cannot work either, because the parties that must not
collide are separate processes — that is the whole problem being solved.

NULL means "no opinion, judge by expiry alone" — which is every existing row, so
there is nothing to backfill.

**Deploy note: apply before deploying either service.** The worker's renewal
claims with this column, and the backend's callback clears it so a brand-new
grant does not inherit a dead one's backoff; on an old schema both raise.

Revision ID: a2e6f4b8c917
Revises: f7c3b9e1a204
Create Date: 2026-07-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a2e6f4b8c917"
down_revision: Union[str, Sequence[str], None] = "f7c3b9e1a204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcp_server",
        sa.Column("oauth_refresh_after", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mcp_server", "oauth_refresh_after")
