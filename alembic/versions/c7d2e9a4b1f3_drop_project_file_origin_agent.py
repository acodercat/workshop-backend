"""drop the AGENT value from project_file_origin

The agent no longer saves files to a project — what a project holds is the
user's to decide — so nothing writes ``origin = AGENT``. Postgres has no
``ALTER TYPE ... DROP VALUE``, so the enum type is swapped, as
``b5e1c9d3a720`` does for ``user_status``:

1. Re-label any leftover ``AGENT`` row ``SESSION_FILE``: it came from a
   session and carries its ``origin_session_id``, which the check
   constraint requires of every non-upload row (none should exist).
2. Drop ``ck_project_file_origin_session_consistent`` around the swap: it
   compares ``origin`` with a literal of the old type, and would not
   re-parse against the new one.

Revision ID: c7d2e9a4b1f3
Revises: b8e2d4f6a3c1
Create Date: 2026-11-03

"""

from typing import Sequence, Union

from alembic import op

revision: str = "c7d2e9a4b1f3"
down_revision: Union[str, Sequence[str], None] = "b8e2d4f6a3c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENUM_NAME = "project_file_origin"
_CHECK_NAME = "ck_project_file_origin_session_consistent"


def _swap_enum(values: tuple[str, ...]) -> None:
    op.drop_constraint(_CHECK_NAME, "project_file", type_="check")
    op.execute(f"ALTER TYPE {_ENUM_NAME} RENAME TO {_ENUM_NAME}_old")
    op.execute(f"CREATE TYPE {_ENUM_NAME} AS ENUM ({', '.join(repr(v) for v in values)})")
    op.execute(
        f"ALTER TABLE project_file ALTER COLUMN origin TYPE {_ENUM_NAME} "
        f"USING origin::text::{_ENUM_NAME}"
    )
    op.execute(f"DROP TYPE {_ENUM_NAME}_old")
    op.create_check_constraint(
        _CHECK_NAME, "project_file", "(origin = 'UPLOAD') = (origin_session_id IS NULL)"
    )


def upgrade() -> None:
    op.execute("UPDATE project_file SET origin = 'SESSION_FILE' WHERE origin = 'AGENT'")
    _swap_enum(("UPLOAD", "SESSION_FILE"))


def downgrade() -> None:
    _swap_enum(("UPLOAD", "SESSION_FILE", "AGENT"))
