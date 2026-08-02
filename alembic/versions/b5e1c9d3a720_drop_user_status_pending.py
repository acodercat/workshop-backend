"""drop the PENDING value from user_status

Registration is now invite-gated and a valid invite is the access
grant, so new users are created ``ACTIVE`` — the ``PENDING`` (awaiting
admin approval) state is gone. Postgres has no ``ALTER TYPE ... DROP
VALUE``, so we swap the enum type:

1. Migrate any leftover ``PENDING`` rows to ``ACTIVE`` (none should
   exist on a current deploy, but be safe).
2. Rename the old type aside, create ``user_status`` without PENDING,
   re-point the column (dropping/restoring its default around the
   cast), drop the old type.

Also flips the column ``server_default`` from ``'PENDING'`` to
``'ACTIVE'`` to match the new model default.

Revision ID: b5e1c9d3a720
Revises: a3f1c7e0d9b2
Create Date: 2026-06-23

"""
from typing import Sequence, Union

from alembic import op


revision: str = "b5e1c9d3a720"
down_revision: Union[str, Sequence[str], None] = "a3f1c7e0d9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ENUM_NAME = "user_status"


def _swap_enum(values: tuple[str, ...], default: str) -> None:
    op.execute(f"ALTER TYPE {_ENUM_NAME} RENAME TO {_ENUM_NAME}_old")
    op.execute(
        f"CREATE TYPE {_ENUM_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in values)})"
    )
    op.execute('ALTER TABLE "user" ALTER COLUMN status DROP DEFAULT')
    op.execute(
        'ALTER TABLE "user" ALTER COLUMN status TYPE '
        f"{_ENUM_NAME} USING status::text::{_ENUM_NAME}"
    )
    op.execute(
        f"ALTER TABLE \"user\" ALTER COLUMN status SET DEFAULT '{default}'::{_ENUM_NAME}"
    )
    op.execute(f"DROP TYPE {_ENUM_NAME}_old")


def upgrade() -> None:
    op.execute("UPDATE \"user\" SET status = 'ACTIVE' WHERE status = 'PENDING'")
    _swap_enum(("ACTIVE", "DISABLED", "SUSPENDED"), default="ACTIVE")


def downgrade() -> None:
    _swap_enum(("ACTIVE", "DISABLED", "SUSPENDED", "PENDING"), default="PENDING")
