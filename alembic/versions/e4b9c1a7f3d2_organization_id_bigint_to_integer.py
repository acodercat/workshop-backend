"""organization id: bigint → integer

``organization.id`` shipped as ``BigInteger`` in d8f3b2e6a1c9 while its
owning identity (``user.id``) and the back-reference columns
(``organization.owner_user_id``, ``organization_member.user_id``) are
``Integer``. Organizations are user-scoped — one auto-created personal
org per user plus a handful of future team orgs — so the count never
approaches the 2.1B Integer ceiling. BigInteger here was an asymmetry,
not a capacity decision: high-volume append tables (credit_ledger,
session_turn, credit_reservation) legitimately use BigInteger; identity
tables do not.

Narrows three coupled columns so the PK and every FK that points at it
share one width:

- ``organization.id``               (PK)
- ``organization_member.organization_id``  (FK → organization.id, CASCADE)
- ``user.organization_id``          (FK → organization.id, RESTRICT)

Postgres rejects an in-place type change on a column referenced by a
foreign key, so both FKs are dropped, the three columns narrowed, then
the FKs recreated with their original ON DELETE behaviour. The default
PG constraint names (``<table>_<column>_fkey``) are reused — the
original migration created the FKs inline via ``sa.ForeignKey`` and the
Base metadata carries no naming_convention, so these are the live names.

The identity sequence is left as-is: Postgres sequences are always
int8 internally and the column simply assignment-casts on insert.
Values stay far below 2³¹, so no sequence surgery is needed. The
bigint→integer column conversion is an implicit assignment cast (no
``USING`` clause required) and is safe because every existing id fits.

Revision ID: e4b9c1a7f3d2
Revises: d8f3b2e6a1c9
Create Date: 2026-05-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4b9c1a7f3d2"
down_revision: Union[str, Sequence[str], None] = "d8f3b2e6a1c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_MEMBER_FK = "organization_member_organization_id_fkey"
_USER_FK = "user_organization_id_fkey"


def upgrade() -> None:
    op.drop_constraint(_MEMBER_FK, "organization_member", type_="foreignkey")
    op.drop_constraint(_USER_FK, "user", type_="foreignkey")

    op.alter_column(
        "organization", "id",
        existing_type=sa.BigInteger(), type_=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        "organization_member", "organization_id",
        existing_type=sa.BigInteger(), type_=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        "user", "organization_id",
        existing_type=sa.BigInteger(), type_=sa.Integer(),
        existing_nullable=True,
    )

    op.create_foreign_key(
        _MEMBER_FK, "organization_member", "organization",
        ["organization_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        _USER_FK, "user", "organization",
        ["organization_id"], ["id"], ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(_MEMBER_FK, "organization_member", type_="foreignkey")
    op.drop_constraint(_USER_FK, "user", type_="foreignkey")

    op.alter_column(
        "user", "organization_id",
        existing_type=sa.Integer(), type_=sa.BigInteger(),
        existing_nullable=True,
    )
    op.alter_column(
        "organization_member", "organization_id",
        existing_type=sa.Integer(), type_=sa.BigInteger(),
        existing_nullable=False,
    )
    op.alter_column(
        "organization", "id",
        existing_type=sa.Integer(), type_=sa.BigInteger(),
        existing_nullable=False,
    )

    op.create_foreign_key(
        _MEMBER_FK, "organization_member", "organization",
        ["organization_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        _USER_FK, "user", "organization",
        ["organization_id"], ["id"], ondelete="RESTRICT",
    )
