"""skill: add display_name_i18n for per-locale official-skill names

Nullable ``{locale: name}`` map on ``skill``. Populated only for OFFICIAL
skills whose SKILL.md ships a ``metadata.display_name`` map; CUSTOM skills and
untranslated official skills leave it NULL and fall back to ``display_name``.
The API resolves it against the request locale (see ``app.services.skill_i18n``),
so localizing a skill name is a backend/bundle change — no frontend edit.

Additive + reversible: a nullable column, no backfill.

Revision ID: d4a8f1c6e2b9
Revises: c6e2a9f4b1d7
Create Date: 2026-07-09

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "d4a8f1c6e2b9"
down_revision: Union[str, Sequence[str], None] = "c6e2a9f4b1d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "skill",
        sa.Column(
            "display_name_i18n",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("skill", "display_name_i18n")
