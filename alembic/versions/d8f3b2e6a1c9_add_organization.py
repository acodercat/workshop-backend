"""organization + organization_member + user.organization_id

Introduces the **organization** concept, mirroring the OpenAI /
Anthropic model: every user belongs to at least one org. Personal
orgs are auto-created at registration (one user, one org, owner
role); team orgs are reserved for future B2B work and require no
schema change beyond this migration.

This migration ships **identity** only. The credit ledger continues
to attach to ``user_id`` — a future migration will move billing to
the org boundary if and when B2B lands.

Forward shape:

1. Create ``organization`` (with FK to ``user.id``) and
   ``organization_member`` (UNIQUE on the org × user pair). The two
   PG enums (``organization_kind`` + ``organization_member_role``)
   are auto-created by ``sa.Enum`` inside ``op.create_table`` —
   matching the project convention established by the
   ``message_feedback`` and credit-system migrations.
2. Add ``user.organization_id`` as a nullable FK. The column is
   nullable because of a circular FK at row-creation time (the org
   needs a valid ``owner_user_id``, so the user must exist first);
   every production code path treats it as NOT NULL after
   registration's same-transaction post-org UPDATE completes.
3. Backfill: for each existing user, create a personal org named
   ``"Personal"`` with the user as ``OWNER`` and link the user back.
   Done in a single CTE-chained SQL statement so it stays atomic
   on partial failure.

Revision ID: d8f3b2e6a1c9
Revises: c2f4a8d1e9b3
Create Date: 2026-05-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8f3b2e6a1c9"
down_revision: Union[str, Sequence[str], None] = "c2f4a8d1e9b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_KIND_NAME = "organization_kind"
_KIND_VALUES = ("PERSONAL", "TEAM")

_ROLE_NAME = "organization_member_role"
_ROLE_VALUES = ("OWNER", "ADMIN", "MEMBER")


def upgrade() -> None:
    # 1) organization — ``sa.Enum`` auto-creates the PG ``organization_kind``
    #    type on first use; we don't pre-create it via ``op.execute``.
    op.create_table(
        "organization",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(*_KIND_VALUES, name=_KIND_NAME),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            sa.Integer,
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_organization_owner_user_id",
        "organization",
        ["owner_user_id"],
    )

    # 2) organization_member — likewise auto-creates ``organization_member_role``.
    op.create_table(
        "organization_member",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.BigInteger,
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.Enum(*_ROLE_VALUES, name=_ROLE_NAME),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "organization_id", "user_id",
            name="uq_organization_member",
        ),
    )
    op.create_index(
        "ix_organization_member_user_id",
        "organization_member",
        ["user_id"],
    )

    # 3) user.organization_id (nullable, see migration docstring for why)
    op.add_column(
        "user",
        sa.Column(
            "organization_id",
            sa.BigInteger,
            sa.ForeignKey("organization.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )

    # 4) Backfill: every existing user gets a personal org + OWNER
    #    membership, and is linked to the new org via
    #    ``user.organization_id``. Single atomic statement via CTE
    #    chaining; the unreferenced ``inserted_members`` CTE still
    #    executes because Postgres always runs the side-effecting
    #    CTEs in a WITH chain.
    op.execute(
        """
        WITH new_orgs AS (
            INSERT INTO organization (name, kind, owner_user_id)
            SELECT 'Personal',
                   'PERSONAL'::organization_kind,
                   id
            FROM "user"
            ORDER BY id
            RETURNING id AS organization_id, owner_user_id AS user_id
        ),
        inserted_members AS (
            INSERT INTO organization_member (organization_id, user_id, role)
            SELECT organization_id, user_id, 'OWNER'::organization_member_role
            FROM new_orgs
        )
        UPDATE "user"
        SET organization_id = new_orgs.organization_id
        FROM new_orgs
        WHERE "user".id = new_orgs.user_id
        """
    )


def downgrade() -> None:
    op.drop_column("user", "organization_id")
    op.drop_index(
        "ix_organization_member_user_id",
        table_name="organization_member",
    )
    op.drop_table("organization_member")
    op.drop_index(
        "ix_organization_owner_user_id",
        table_name="organization",
    )
    op.drop_table("organization")
    # ``sa.Enum(name=...).drop`` mirrors the user_feedback migration —
    # alembic doesn't auto-drop the PG type on table drop, so we
    # explicitly DROP TYPE. ``checkfirst=True`` makes downgrade idempotent.
    sa.Enum(name=_ROLE_NAME).drop(op.get_bind(), checkfirst=True)
    sa.Enum(name=_KIND_NAME).drop(op.get_bind(), checkfirst=True)
