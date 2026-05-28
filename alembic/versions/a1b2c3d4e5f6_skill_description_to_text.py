"""skill: widen description column to TEXT

VARCHAR(512) was too tight for real-world SKILL.md descriptions (Agent
Skills spec allows 1024, and some descriptions with multiline YAML
approach that). TEXT has identical performance in Postgres (same varlena
storage); length enforcement stays in the application layer (Pydantic
schema + ``_clip``). Instant catalog-only ALTER, no table rewrite.

Revision ID: a1b2c3d4e5f6
Revises: f7a3c2e9b4d1
Create Date: 2026-05-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f7a3c2e9b4d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "skill",
        "description",
        type_=sa.Text(),
        existing_type=sa.String(512),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "skill",
        "description",
        type_=sa.String(512),
        existing_type=sa.Text(),
        existing_nullable=False,
    )
