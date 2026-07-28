"""mcp_server.advertised_icon_url

The icon a server advertises for itself in ``serverInfo.icons`` (MCP SEP-973,
spec 2025-11-25), captured whenever we probe. Lets a Connector the user defined
themselves carry its own artwork instead of the generic plug glyph — until now
only catalog entries had an icon, and that one is set by hand.

Stored per Connector rather than read live because the client never talks to the
MCP server; it renders from what the backend cached at probe time, exactly like
``tools_preview``.

Named apart from ``mcp_server_catalog.icon_url``: that one is operator-curated
artwork for a published entry, this one is whatever the server says about
itself. They meet in the client, which prefers the curated one, so identical
names there would read as a coin toss.

Nullable: most servers advertise nothing, and the UI already falls back — first
to the operator-curated catalog icon, then to the built-in glyph.

No deploy ordering constraint: the worker neither reads nor writes this column.

Revision ID: b5c9e2f7a184
Revises: a7d2f4e9c613
Create Date: 2026-07-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b5c9e2f7a184"
down_revision: Union[str, Sequence[str], None] = "a7d2f4e9c613"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcp_server", sa.Column("advertised_icon_url", sa.String(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("mcp_server", "advertised_icon_url")
