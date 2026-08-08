"""align user.phone unique as constraint

The last straggler of ``b1c7e29f4d8a``'s cleanup: ``user.phone`` is
``unique=True`` on the model (SQLAlchemy → ``UniqueConstraint``) but
lives in the DB as the **partial** unique index ``ix_user_phone``
(``WHERE phone IS NOT NULL``), so ``alembic check`` flags the
declaration-style mismatch on every run.

Same reasoning as that migration's ``ix_user_email`` path: Postgres
refuses ``USING INDEX`` on partial indexes, so build a fresh full
unique constraint and drop the partial. Functionally equivalent on a
nullable column — the default ``NULLS DISTINCT`` means the many
phone-less accounts were allowed before and remain allowed after;
non-NULL uniqueness is preserved. The constraint name follows
Postgres' default ``<table>_<column>_key`` form so ``alembic check``
agrees with the unnamed ``UniqueConstraint`` the model synthesises.

Revision ID: a9c4e2f7d1b8
Revises: e2b7d4f9a1c6
Create Date: 2026-08-06
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect


revision: str = "a9c4e2f7d1b8"
down_revision: Union[str, Sequence[str], None] = "e2b7d4f9a1c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_phone() -> bool:
    """Whether this deployment still has the column at all.

    It does not here: the phone channel went in ``5fcf505`` and the column
    with it in ``b7e2d9a4c1f5``, so this revision arrives from master with
    nothing to align and its first statement would fail on a column that is
    not there — taking every migration queued behind it down too.

    A later revision cannot undo an earlier one's failure, so the guard has
    to live in the revision itself. Master keeps the column and takes both
    branches unchanged.
    """
    columns = {c["name"] for c in inspect(op.get_bind()).get_columns("user")}
    return "phone" in columns


def upgrade() -> None:
    if not _has_phone():
        return
    op.execute('ALTER TABLE "user" ADD CONSTRAINT user_phone_key UNIQUE (phone)')
    op.execute("DROP INDEX ix_user_phone")


def downgrade() -> None:
    if not _has_phone():
        return
    op.execute('ALTER TABLE "user" DROP CONSTRAINT user_phone_key')
    op.execute(
        "CREATE UNIQUE INDEX ix_user_phone "
        'ON "user" (phone) WHERE phone IS NOT NULL'
    )
