"""align unique indexes as constraints

Cosmetic-only drift cleanup. Four columns were declared as
``unique=True`` on the model (SQLAlchemy → ``UniqueConstraint``) but
landed in the DB as **named unique indexes** from earlier hand-written
migrations. Functionally equivalent in Postgres (constraints are
implemented via unique indexes anyway), but ``alembic check`` flags
the declaration-style mismatch on every run.

Affected columns:

- ``"user".username``         (``ix_user_username``         → ``user_username_key``)
- ``session_files.file_key``  (``ix_session_files_file_key``  → ``session_files_file_key_key``)
- ``session_share.token``     (``ix_session_share_token``     → ``session_share_token_key``)
- ``"user".email``  — special: source index is partial
  (``WHERE email IS NOT NULL``). Postgres refuses ``USING INDEX`` on
  partial indexes, so we build a fresh full unique constraint and
  drop the old partial. Functionally equivalent on a nullable column
  because Postgres treats NULL as distinct in unique constraints
  (default ``NULLS DISTINCT``) — multiple NULL emails were allowed
  before and remain allowed after; non-NULL uniqueness is preserved.

**Upgrade path for non-partial indexes**:
``ALTER TABLE ... ADD CONSTRAINT <name> UNIQUE USING INDEX <ix_name>``
reuses the existing btree and renames it to the constraint name.
Catalog-only metadata flip — no rebuild, no uniqueness gap, no risk
on a production DB taking concurrent writes. Postgres docs:
https://www.postgresql.org/docs/current/sql-altertable.html

**Upgrade path for the partial ``ix_user_email``**:
``ALTER TABLE "user" ADD CONSTRAINT user_email_key UNIQUE (email)``
creates a fresh full unique index + constraint atomically. The new
constraint enforces the same uniqueness rule the partial index did
(non-NULL emails unique, multiple NULLs allowed). Then drop the
partial.

The constraint names follow Postgres' default ``<table>_<column>_key``
form so that ``alembic check`` agrees with the unnamed
``UniqueConstraint`` SQLAlchemy synthesises from ``unique=True`` on
the column.

Revision ID: b1c7e29f4d8a
Revises: 9e3a8b714f02
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op


revision: str = "b1c7e29f4d8a"
down_revision: Union[str, Sequence[str], None] = "9e3a8b714f02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, old_index_name, new_constraint_name) for the three
# non-partial indexes that can be promoted in place.
_PROMOTABLE = (
    ('"user"',       "username", "ix_user_username",          "user_username_key"),
    ("session_files", "file_key", "ix_session_files_file_key", "session_files_file_key_key"),
    ("session_share", "token",    "ix_session_share_token",    "session_share_token_key"),
)


def upgrade() -> None:
    # Three non-partial indexes → in-place promotion.
    for table, _col, idx_name, constraint_name in _PROMOTABLE:
        op.execute(
            f"ALTER TABLE {table} "
            f"ADD CONSTRAINT {constraint_name} "
            f"UNIQUE USING INDEX {idx_name}"
        )

    # Email is special — the source index is partial, so build a
    # fresh full unique constraint and drop the partial separately.
    op.execute(
        'ALTER TABLE "user" ADD CONSTRAINT user_email_key UNIQUE (email)'
    )
    op.execute("DROP INDEX ix_user_email")


def downgrade() -> None:
    # Reverse the email-specific path first.
    op.execute('ALTER TABLE "user" DROP CONSTRAINT user_email_key')
    op.execute(
        'CREATE UNIQUE INDEX ix_user_email ON "user" (email) '
        "WHERE email IS NOT NULL"
    )

    # Reverse the in-place promotions. Dropping the constraint also
    # drops its implementing index, so the index is re-created
    # afterward to restore the pre-upgrade name. Downgrade *does*
    # have a brief uniqueness gap here — acceptable since downgrades
    # are rare and controlled.
    for table, col, idx_name, constraint_name in _PROMOTABLE:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint_name}")
        op.execute(f"CREATE UNIQUE INDEX {idx_name} ON {table} ({col})")
