"""session.id + message.session_id → native UUID

Revision ID: 9c4a8b1e7d62
Revises: f0af49a0af41
Create Date: 2026-04-19

Session primary keys were stored as 32-char hex strings (``uuid.uuid4().hex``
with no dashes). Switch to Postgres' native ``uuid`` type — 16-byte storage,
real type validation, and the standard 36-char dashed representation on
the wire.

``message.session_id`` is a foreign key to ``session.id`` and must change
in lockstep. Existing 32-char hex values are cast via ``'-'`` insertion
so Postgres accepts them as UUIDs.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "9c4a8b1e7d62"
down_revision: Union[str, Sequence[str], None] = "f0af49a0af41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _hex_to_uuid(column: str) -> str:
    """Return a SQL expression that inserts the standard 8-4-4-4-12 dashes
    into a 32-char hex string and casts to ``uuid``. Works for every value
    produced by ``uuid.uuid4().hex``."""
    return (
        f"(substring({column} from 1 for 8) || '-' ||"
        f" substring({column} from 9 for 4) || '-' ||"
        f" substring({column} from 13 for 4) || '-' ||"
        f" substring({column} from 17 for 4) || '-' ||"
        f" substring({column} from 21 for 12))::uuid"
    )


_FK = "message_session_id_fkey"


def upgrade() -> None:
    # Postgres won't let us alter the PK or the FK column while they're
    # bound by the constraint — drop it, alter both, re-add.
    op.drop_constraint(_FK, "message", type_="foreignkey")
    op.execute(f"ALTER TABLE session ALTER COLUMN id TYPE uuid USING {_hex_to_uuid('id')}")
    op.execute(f"ALTER TABLE message ALTER COLUMN session_id TYPE uuid USING {_hex_to_uuid('session_id')}")
    op.create_foreign_key(
        _FK, "message", "session",
        ["session_id"], ["id"], ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(_FK, "message", type_="foreignkey")
    op.execute("ALTER TABLE message ALTER COLUMN session_id TYPE varchar(32) USING replace(session_id::text, '-', '')")
    op.execute("ALTER TABLE session ALTER COLUMN id TYPE varchar(32) USING replace(id::text, '-', '')")
    op.create_foreign_key(
        _FK, "message", "session",
        ["session_id"], ["id"], ondelete="CASCADE",
    )
