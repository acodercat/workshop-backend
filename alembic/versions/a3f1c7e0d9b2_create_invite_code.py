"""create invite_code + invite_redemption

Invite/referral codes redeemed at registration (see
``app/models/invite_code.py`` and ``invite_redemption.py``).

``invite_code`` follows the skill/recipe ``origin`` shape: ``SYSTEM``
codes (operator-issued, ``owner_user_id IS NULL``) seed the cold
start; ``REFERRAL`` codes (``owner_user_id`` set) are a user's stable
personal code, capped to one per user by a partial-unique index. A
consistency CHECK keeps ``origin`` and ``owner_user_id`` in agreement,
and a second CHECK keeps ``used_count`` within ``max_uses``.

``invite_redemption`` is the append-only audit behind the cached
``invite_code.used_count`` — one row per invited account, unique on
``invitee_user_id``. All FKs are ``RESTRICT`` so provenance can't be
silently deleted.

Purely additive — two new tables and one new enum — so existing rows
are untouched.

Revision ID: a3f1c7e0d9b2
Revises: f2a4c6e8b1d3
Create Date: 2026-06-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a3f1c7e0d9b2"
down_revision: Union[str, Sequence[str], None] = "f2a4c6e8b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ORIGIN_NAME = "invite_code_origin"
_ORIGIN_VALUES = ("SYSTEM", "REFERRAL")


def upgrade() -> None:
    # Raw SQL CREATE TYPE, then reference it with create_type=False on
    # the column — matches the project convention (see recipe).
    op.execute(
        f"CREATE TYPE {_ORIGIN_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _ORIGIN_VALUES)})"
    )

    op.create_table(
        "invite_code",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=32), nullable=False, unique=True),
        sa.Column(
            "origin",
            postgresql.ENUM(*_ORIGIN_VALUES, name=_ORIGIN_NAME, create_type=False),
            nullable=False,
            server_default=sa.text("'REFERRAL'"),
        ),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column(
            "used_count", sa.Integer(), nullable=False, server_default=sa.text("0"),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true"),
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_invite_code_origin_owner_consistent",
        "invite_code",
        "(origin = 'SYSTEM' AND owner_user_id IS NULL) "
        "OR (origin = 'REFERRAL' AND owner_user_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_invite_code_used_within_max",
        "invite_code",
        "used_count >= 0 AND (max_uses IS NULL OR used_count <= max_uses)",
    )
    op.create_index(
        "uq_invite_code_referral_owner",
        "invite_code",
        ["owner_user_id"],
        unique=True,
        postgresql_where=sa.text("origin = 'REFERRAL'"),
    )

    op.create_table(
        "invite_redemption",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "invite_code_id",
            sa.Integer(),
            sa.ForeignKey("invite_code.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "invitee_user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "referrer_user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_invite_redemption_invitee", "invite_redemption", ["invitee_user_id"],
    )
    op.create_index(
        "ix_invite_redemption_invite_code_id", "invite_redemption", ["invite_code_id"],
    )
    op.create_index(
        "ix_invite_redemption_referrer_user_id",
        "invite_redemption",
        ["referrer_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_invite_redemption_referrer_user_id", table_name="invite_redemption",
    )
    op.drop_index(
        "ix_invite_redemption_invite_code_id", table_name="invite_redemption",
    )
    op.drop_constraint(
        "uq_invite_redemption_invitee", "invite_redemption", type_="unique",
    )
    op.drop_table("invite_redemption")

    op.drop_index("uq_invite_code_referral_owner", table_name="invite_code")
    op.drop_constraint(
        "ck_invite_code_used_within_max", "invite_code", type_="check",
    )
    op.drop_constraint(
        "ck_invite_code_origin_owner_consistent", "invite_code", type_="check",
    )
    op.drop_table("invite_code")
    op.execute(f"DROP TYPE IF EXISTS {_ORIGIN_NAME}")
