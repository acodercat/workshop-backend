"""timestamps: convert legacy naive columns to timezone-aware

The schema grew two conventions for "moment in time" columns. Newer
tables/columns use ``TIMESTAMP(timezone=True)`` (``timestamptz`` — the
correct type for an instant: stores UTC, renders in the viewer's zone),
but the older ones (``session`` / ``message`` / ``user`` / the feedback
tables / ``sandbox`` / ``session_files`` / ``session_share``) use a bare
naive ``TIMESTAMP``. ``func.now()`` returns a ``timestamptz``; storing it
into a naive column casts it through the *connection* TimeZone and drops
the offset. ``app/database.py`` sets no timezone, so inserts used the
server ``timezone`` GUC — Asia/Shanghai here — which is why a naive
``session.created_at`` reads as Beijing wall-clock while a tz-aware
``session_turn.created_at`` reads as UTC in a UTC viewer (the 8h gap).

This standardizes every naive column to ``timestamptz``, reinterpreting
the stored wall-clock values **as Asia/Shanghai** to recover the true
instant (see ``SOURCE_TZ``). Columns already ``timestamptz``
(``session_turn.*``, ``credit_*``, ``organization.*``, ``skill.*``,
``user_skill_preference.*``, ``session_context.*``, ``session.deleted_at``)
are intentionally left untouched.

PRE-FLIGHT — run against the target DB before upgrading, and rehearse on
a snapshot first (each ``ALTER COLUMN TYPE`` rewrites the column under an
ACCESS EXCLUSIVE lock; fast at current row counts, but it is a rewrite):

    SHOW timezone;
        -- must be Asia/Shanghai (or PRC). If it is UTC, the naive values
        -- are UTC-based and SOURCE_TZ below must be 'UTC' instead, or
        -- every historical timestamp shifts by 8h.
    SELECT created_at FROM session ORDER BY created_at DESC LIMIT 1;
        -- sanity-check it reads as the expected Beijing wall-clock.

Side effect: the API now serializes these fields with a UTC offset
(``...+00:00``) instead of a naive string. A frontend that parses ISO
offsets (``new Date(s)``) is unaffected; one that assumed naive-local
should be spot-checked.

Revision ID: c7d2f9a4e1b8
Revises: b2e9c4f7a1d6
Create Date: 2026-06-09

"""
from typing import Sequence, Union

from alembic import op

revision: str = "c7d2f9a4e1b8"
down_revision: Union[str, Sequence[str], None] = "b2e9c4f7a1d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The wall-clock zone the existing naive values were written in. Drives
# the ``AT TIME ZONE`` reinterpretation in both directions. See the
# module docstring's PRE-FLIGHT block before trusting this on a DB whose
# server ``timezone`` GUC may not be Asia/Shanghai.
SOURCE_TZ = "Asia/Shanghai"

# Every naive ``timestamp without time zone`` column, as (table, column).
# ``user`` is a reserved word — quoting is applied uniformly below.
_NAIVE_COLUMNS: Sequence[tuple[str, str]] = (
    ("session", "created_at"),
    ("session", "updated_at"),
    ("message", "created_at"),
    ("message_feedback", "created_at"),
    ("message_feedback", "updated_at"),
    ("user_feedback", "created_at"),
    ("user_feedback", "updated_at"),
    ("user", "last_login_at"),
    ("user", "created_at"),
    ("user", "updated_at"),
    ("session_files", "created_at"),
    ("sandbox", "created_at"),
    ("sandbox", "last_used_at"),
    ("session_share", "created_at"),
)


def _retype(table: str, column: str, to_type: str) -> None:
    # ``AT TIME ZONE 'Asia/Shanghai'`` is overloaded by input type:
    #   naive timestamp  -> timestamptz : reads the value AS Shanghai
    #   timestamptz      -> naive       : renders Shanghai wall-clock
    # so the same expression is its own inverse across upgrade/downgrade.
    op.execute(
        f'ALTER TABLE "{table}" '
        f'ALTER COLUMN "{column}" TYPE {to_type} '
        f"USING \"{column}\" AT TIME ZONE '{SOURCE_TZ}'"
    )


def upgrade() -> None:
    for table, column in _NAIVE_COLUMNS:
        _retype(table, column, "timestamptz")


def downgrade() -> None:
    for table, column in _NAIVE_COLUMNS:
        _retype(table, column, "timestamp")
