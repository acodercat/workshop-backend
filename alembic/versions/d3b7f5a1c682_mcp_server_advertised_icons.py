"""mcp_server.advertised_icons — every icon a server offers, not one of them

Replaces ``advertised_icon_url``. That column held whichever icon survived a
first-usable scan at probe time, which threw away the two things the choice
actually depends on.

A server may advertise several (SEP-973): the same mark at different
resolutions, and light- and dark-background variants. Which one is right depends
on the viewer's device pixel ratio and colour scheme, and the backend knows
neither — it is answering a request that may be rendered on a 1x laptop and a 3x
phone in the same minute. Picking here could only be a guess overriding a
decision the browser makes correctly from ``srcset`` and
``prefers-color-scheme``. So the row keeps the candidates and the client hands
them over intact.

Each entry is ``{src, mime_type, sizes, theme}``, sanitized before it lands here:
``src`` is an https or ``data:image/`` URI (other schemes are a MUST-reject),
length and count are bounded, and fields are copied by name so a server cannot
park arbitrary JSON in the column.

Backfilled from the old column so no Connector loses the icon it already shows —
one entry, no ``sizes``, which is exactly what the old value meant. Nothing else
is recoverable; the rest arrives at the next probe.

``NOT NULL DEFAULT '[]'`` rather than nullable, matching ``disabled_tools``: "no
icons" is an empty list and should have one spelling, not two that every reader
has to treat alike.

**Deploy note: apply before the API.** ``ConnectorResponse`` reads the new column
and no longer exposes the old one; a client on the old bundle finds no icon field
and falls back to the built-in glyph, which is the same thing it already does for
a server that advertises nothing. The worker touches neither column.

Revision ID: d3b7f5a1c682
Revises: a2e6f4b8c917
Create Date: 2026-07-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "d3b7f5a1c682"
down_revision: Union[str, Sequence[str], None] = "a2e6f4b8c917"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcp_server",
        sa.Column(
            "advertised_icons",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # One entry carrying the only field the old column had. ``sizes`` and
    # ``theme`` stay absent rather than guessed, which reads to the client as
    # "one candidate, unknown resolution" — the same thing it assumed before.
    op.execute(
        """
        UPDATE mcp_server
           SET advertised_icons = jsonb_build_array(
                   jsonb_build_object('src', advertised_icon_url)
               )
         WHERE advertised_icon_url IS NOT NULL
        """
    )
    op.drop_column("mcp_server", "advertised_icon_url")


def downgrade() -> None:
    op.add_column(
        "mcp_server",
        sa.Column("advertised_icon_url", sa.String(), nullable=True),
    )
    # Back to one icon, chosen the way the old code chose: the first usable one.
    op.execute(
        """
        UPDATE mcp_server
           SET advertised_icon_url = advertised_icons -> 0 ->> 'src'
         WHERE jsonb_array_length(advertised_icons) > 0
        """
    )
    op.drop_column("mcp_server", "advertised_icons")
