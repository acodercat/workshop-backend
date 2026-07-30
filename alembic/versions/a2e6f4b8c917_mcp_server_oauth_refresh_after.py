"""mcp_server.oauth_refresh_after

The earliest moment the token refresher may try this Connector again.

Until now that answer was inferred from ``oauth_tokens_expires_at``: a row was
due whenever its access token expired within ``connector_refresh_ahead_seconds``
(default 600). That is a fine rule for a token that outlives the window and a
broken one for a token that does not — and MCP's authorization spec asks
authorization servers to prefer short-lived access tokens, so the second case is
ordinary, not hostile. A 300-second token is *still* inside the look-ahead the
instant it is written, so it is due again on the very next tick, and because the
query orders by soonest expiry it sits ahead of every healthy row. Enough such
Connectors and nothing else is ever selected: the normal tokens behind them
expire while their provider is refreshed once a minute for nothing.

The in-process map this replaces could not fix that. It only ever recorded
failures, so it had nothing to say about the case above; it was per process, so
two replicas each rotated the same grant in turn; and it did not survive a
restart. Widening it to cover successes as well is the thing that does not work:
a fixed window is either shorter than the tick interval, in which case it changes
nothing, or longer than the shortest legitimate token lifetime, in which case it
holds a 300-second grant out past its own expiry. There is no value between.

A column is what both halves wanted. It is written from the *new* token's own
lifetime after a success and from a flat backoff after a failure, it is visible
to every replica, and it makes "when is this due" a single fact on the row
rather than something two different pieces of code infer separately.

NULL means "no opinion, judge by expiry alone" — which is every existing row, so
there is nothing to backfill.

**Deploy note: apply before deploying the backend.** The refresher's due query
filters on this column; on an old schema every tick raises and no Connector's
grant is renewed at all.

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
