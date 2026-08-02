"""add user.phone

China-mainland mobile for SMS-OTP registration / login. Mirrors the
``email`` column: nullable + UNIQUE (Postgres treats NULLs as distinct,
so the constraint only binds non-NULL rows).

Revision ID: e5c1a9b7d2f4
Revises: d8b3f6a1c402
Create Date: 2026-06-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5c1a9b7d2f4"
down_revision: Union[str, Sequence[str], None] = "d8b3f6a1c402"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user", sa.Column("phone", sa.String(length=20), nullable=True))
    # UNIQUE only on non-NULL rows via a partial index — mirrors the
    # ``email`` column (migration ``12dc6d69a9ad``). Postgres' regular
    # UNIQUE allows multiple NULLs already, but we make the intent
    # explicit. ``ix_user_phone`` is the same name this column carried
    # before it was dropped in ``12dc6d69a9ad``.
    op.create_index(
        "ix_user_phone",
        "user",
        ["phone"],
        unique=True,
        postgresql_where=sa.text("phone IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_user_phone", table_name="user")
    op.drop_column("user", "phone")
