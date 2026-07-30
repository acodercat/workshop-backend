"""mcp_server.last_connect_failed

Whether the most recent turn that tried failed to connect to this Connector.

``oauth_reauthorization_required`` is the provider's verdict on an OAuth
*grant*, so by construction it can never fire for a BEARER / HEADER / NONE
Connector. Those had no health signal at all: a server that stops resolving
contributes no tools every turn while the UI keeps reporting "connected · N
tools" off a cached count, because ``authorized`` is hard-coded true for
non-OAuth rows. This is the missing half — the verdict about *reaching* the
server, which every auth type can fail.

The two are orthogonal and both are kept: one says the grant is dead and only a
trip through the browser fixes it, the other says the server could not be
reached. The reason is left to the worker log rather than a free-text column,
which would leak an upstream server's internals into a client-facing response.

Written by the worker on every attempt, not only on a change against the row it
read — that snapshot is not evidence about the column, since a concurrent turn
may have marked the Connector broken since, and skipping the write because *our*
snapshot said "healthy" would leave a stale verdict standing over a Connector we
just proved works. So budget one indexed single-row UPDATE per connected
Connector per turn: ten concurrent healthy turns for one user cost ten writes,
not zero. Each is fenced on the value that turn read (``AND last_connect_failed
= …``), which makes a stolen race a rowcount-0 no-op rather than an inversion.

Skipping a Connector is not a verdict: an exhausted connect budget leaves the
flag untouched, because nothing was learned about the server.

Its sibling ``oauth_reauthorization_required`` is deliberately *not* written by
the worker. A turn can see that a server refused its access token, not that the
grant behind it is dead — that is answered where a refresh is attempted, in the
backend's token refresher.

**Deploy note: apply before deploying either the backend or the worker.** The
API and the background refresher both read these columns, and the worker SELECTs
this one in its per-turn Connector resolve — an old schema makes it resolve
*zero* Connectors.

Revision ID: e4a9c2b7d503
Revises: c8f4a1d6e207
Create Date: 2026-07-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e4a9c2b7d503"
down_revision: Union[str, Sequence[str], None] = "c8f4a1d6e207"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcp_server",
        sa.Column(
            "last_connect_failed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("mcp_server", "last_connect_failed")
