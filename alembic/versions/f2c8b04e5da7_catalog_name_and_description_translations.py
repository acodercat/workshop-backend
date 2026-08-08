"""mcp_server_catalog — a name and description per locale

The catalog is the one place the product shows operator-authored text to a
user, so it is the one place the UI language cannot reach: next-intl covers
the shell, and an entry's name and description came out of the database in
whatever language they were typed.

The shape is the skills catalog's, now shared as ``services/localized_text``:
the existing column keeps the canonical text and a nullable ``{locale: text}``
map sits beside it. Untranslated rows stay exactly as they are — NULL maps,
and every read falls back to the column — so this migration adds two columns
and changes no data.

A side table was the alternative. It buys indexing and referential integrity
on the locale, neither of which display text on a hand-curated shortlist
needs, and costs a join on every catalog read.

Revision ID: f2c8b04e5da7
Revises: a3d7f61b8e04
Create Date: 2026-08-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "f2c8b04e5da7"
down_revision: Union[str, Sequence[str], None] = "a3d7f61b8e04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for column in ("name_i18n", "description_i18n"):
        op.add_column(
            "mcp_server_catalog",
            sa.Column(column, postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )


def downgrade() -> None:
    # The translations go with the columns. Nothing else held them, and the
    # canonical text every locale falls back to is untouched, so the catalog
    # keeps working in one language.
    for column in ("description_i18n", "name_i18n"):
        op.drop_column("mcp_server_catalog", column)
