"""mcp_server_catalog — the URL is the identity, and ``key`` goes

``key`` was a third name for a row that already had two: ``id`` for machines,
``name`` for people. It was meant to be the stable operator handle, but the
only thing it uniquely did was carry this table's single unique constraint —
and that constraint belongs on ``url``. Two entries pointing at one server are
the same Connector offered twice, whatever they are labelled; two entries
sharing a display name are not necessarily anything.

``name`` stays deliberately non-unique: it is the copy shown to users and
copied onto their row, and an operator renaming one entry must not collide
with another.

The unique index goes on before the column comes off, so a catalog that
already holds two rows for one URL fails here with both still present rather
than losing the handle that tells them apart. That is a real possibility this
migration cannot resolve on its own — which of the two is the keeper is an
operator's call, so it stops and asks rather than guessing.

Revision ID: b8f2a91c47d3
Revises: c1e7a4b2f983
Create Date: 2026-08-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b8f2a91c47d3"
down_revision: Union[str, Sequence[str], None] = "c1e7a4b2f983"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_mcp_server_catalog_url", "mcp_server_catalog", ["url"]
    )
    op.drop_column("mcp_server_catalog", "key")


def downgrade() -> None:
    # Nullable first: the old column was NOT NULL and unique, and there is no
    # default that satisfies both. Seeding from ``id`` gives every row a
    # distinct value that is honestly a placeholder — the slugs themselves are
    # not recoverable, they were dropped.
    op.add_column(
        "mcp_server_catalog", sa.Column("key", sa.String(length=64), nullable=True)
    )
    op.execute("UPDATE mcp_server_catalog SET key = 'entry_' || id")
    op.alter_column("mcp_server_catalog", "key", nullable=False)
    op.create_unique_constraint(
        "uq_mcp_server_catalog_key", "mcp_server_catalog", ["key"]
    )
    op.drop_constraint(
        "uq_mcp_server_catalog_url", "mcp_server_catalog", type_="unique"
    )
