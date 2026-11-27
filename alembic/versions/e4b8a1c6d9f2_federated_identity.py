"""federate sign-in: user_identity, identity_session, and what they retire

Sign-in moves to an external identity provider (the Meeting service). A
local ``user`` row stays the key every business table hangs from; these
tables say which provider account it belongs to and which sessions are
live. See ``app/models/user_identity.py`` and
``app/models/identity_session.py``.

Three things on ``user`` stop being credentials and change to match:

- ``password_hash`` becomes nullable. An account created by sign-in has no
  local password. Existing rows keep theirs; nothing reads it.
- ``user_email_key`` is dropped. The address is now a copy of what the
  provider reports, and an account that predates federation may hold the
  same one — with the constraint in place that row would refuse the
  provider's own user at their first sign-in.
- ``token_version`` is dropped. Signing a user out is revoking their
  ``identity_session`` rows; a second, account-wide counter would be a
  second way to do the same thing.

No data moves: accounts created before this have no ``user_identity`` row
and so cannot sign in, which is the intended state until someone decides
how they are claimed.

**Downgrade** restores the constraints, and can only do so on a database
where they still hold — it deletes nothing to make them. Once federated
accounts exist it fails on the first one that no longer does; remove those
accounts first.

Revision ID: e4b8a1c6d9f2
Revises: c7d2e9a4b1f3
Create Date: 2026-11-18

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e4b8a1c6d9f2"
down_revision: Union[str, Sequence[str], None] = "c7d2e9a4b1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_identity",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("issuer", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "issuer", "subject", name="uq_user_identity_subject"),
    )
    op.create_index("ix_user_identity_user_id", "user_identity", ["user_id"])

    op.create_table(
        "identity_session",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("identity_id", sa.Integer(), nullable=False),
        sa.Column("credential", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "checked_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "verified_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["identity_id"], ["user_identity.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_identity_session_user_id", "identity_session", ["user_id"])

    op.alter_column("user", "password_hash", existing_type=sa.String(length=255), nullable=True)
    op.drop_constraint("user_email_key", "user", type_="unique")
    op.drop_column("user", "token_version")


def downgrade() -> None:
    # Backfilled through a temporary default, then left without one, as the
    # column was before.
    op.add_column(
        "user",
        sa.Column("token_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.alter_column("user", "token_version", server_default=None)
    op.create_unique_constraint("user_email_key", "user", ["email"])
    op.alter_column("user", "password_hash", existing_type=sa.String(length=255), nullable=False)

    op.drop_index("ix_identity_session_user_id", table_name="identity_session")
    op.drop_table("identity_session")
    op.drop_index("ix_user_identity_user_id", table_name="user_identity")
    op.drop_table("user_identity")
