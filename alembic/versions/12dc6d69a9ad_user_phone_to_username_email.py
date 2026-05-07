"""user: drop phone; add username (unique) + email (unique, nullable)

The login identifier moves from a misnamed ``phone`` column (which has
been holding usernames like ``"wang"`` / ``"hebei"``) to a properly-named
``username`` column. ``email`` joins the schema to support the new
email-verification registration flow; existing rows have no email yet
so the column is nullable, with the contract "newly registered users
always have one; legacy users may be NULL until they update their
profile".

Migration order matters — add nullable, backfill, then constrain:

1. ``ADD COLUMN username VARCHAR(30) NULL``
2. ``UPDATE "user" SET username = phone WHERE phone IS NOT NULL`` —
   every existing user already had a phone (used as login id), so the
   1:1 mapping is lossless.
3. ``ALTER COLUMN username SET NOT NULL`` + ``CREATE UNIQUE INDEX``
4. ``ADD COLUMN email VARCHAR(255) NULL`` + ``CREATE UNIQUE INDEX``
   (partial: only on non-NULL rows)
5. ``DROP COLUMN phone``

Revision ID: 12dc6d69a9ad
Revises: 14de0b363bda
Create Date: 2026-05-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "12dc6d69a9ad"
down_revision: Union[str, Sequence[str], None] = "14de0b363bda"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: add nullable username
    op.add_column("user", sa.Column("username", sa.String(length=30), nullable=True))

    # Step 2: backfill from phone (every existing row had a phone value)
    op.execute('UPDATE "user" SET username = phone WHERE phone IS NOT NULL')

    # Step 3: enforce NOT NULL + UNIQUE on username
    op.alter_column("user", "username", nullable=False)
    op.create_index(
        "ix_user_username",
        "user",
        ["username"],
        unique=True,
    )

    # Step 4: email nullable (legacy users may not have one); UNIQUE
    # only on non-NULL rows via a partial index — Postgres' regular
    # UNIQUE allows multiple NULLs but we make the intent explicit.
    op.add_column("user", sa.Column("email", sa.String(length=255), nullable=True))
    op.create_index(
        "ix_user_email",
        "user",
        ["email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )

    # Step 5: drop phone column (and its UNIQUE index implicitly)
    op.drop_column("user", "phone")


def downgrade() -> None:
    op.add_column("user", sa.Column("phone", sa.String(length=20), nullable=True))
    op.create_index(
        "ix_user_phone",
        "user",
        ["phone"],
        unique=True,
    )
    op.execute('UPDATE "user" SET phone = username')

    op.drop_index("ix_user_email", table_name="user")
    op.drop_column("user", "email")

    op.drop_index("ix_user_username", table_name="user")
    op.drop_column("user", "username")
