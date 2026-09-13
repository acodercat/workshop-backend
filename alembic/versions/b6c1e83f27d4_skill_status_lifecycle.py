"""skill.status — split the listing's lifecycle out of ``enabled``

``skill.enabled`` had grown three meanings, one per origin: an operator's
staging switch on ``SYSTEM``, a platform default nobody ever moved on
``OFFICIAL``, and the owner's own toggle on ``CUSTOM``. This splits the
two questions that were sharing it — *is the listing live* (``status``,
here) and *does this user want it* (``user_skill_preference``, which
already existed and now carries every origin).

Additive by design. ``enabled`` survives this revision because
``core/agent-worker`` is shared with the metaseq deployment and still gates
on ``COALESCE(pref.enabled, skill.enabled)``; it cannot read ``status``
until both databases have the column. The backfill therefore leaves
``enabled`` at the value that makes the old formula agree with the new
one, and a follow-up revision drops it once that worker has shipped —
expand, migrate, contract.

Zero behaviour change on its own: every combination that exists when
this runs resolves the same way under both formulas.

**Do not retire a listing until the worker ships.** Until it reads
``status``, ``STAGED`` and ``RETIRED`` only bite on the paths the
backend owns, and no value of the mirror column can close the gap —
``COALESCE(pref.enabled, skill.enabled)`` never reads that column for a
user who has a preference row, so a retired skill keeps reaching exactly
the users who had turned it on. New listings are unaffected: nobody has
a preference row for one yet.

Revision ID: b6c1e83f27d4
Revises: c8f2a5e1d94b
Create Date: 2026-09-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6c1e83f27d4"
down_revision: Union[str, Sequence[str], None] = "c8f2a5e1d94b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENUM_NAME = "skill_status"
_VALUES = ("STAGED", "PUBLISHED", "RETIRED")


def upgrade() -> None:
    op.execute(f"CREATE TYPE {_ENUM_NAME} AS ENUM ({', '.join(repr(v) for v in _VALUES)})")

    # A temporary default so the NOT NULL column can be added to a
    # populated table; dropped at the end, because the two INSERT sites
    # disagree about what a new row should be (a CUSTOM upload is live at
    # once, a platform one is staged) and no single default is right for
    # both. The column this replaces is the cautionary tale: its
    # ``server_default`` of true was the opposite of what uploads wanted.
    op.add_column(
        "skill",
        sa.Column(
            "status",
            sa.Enum(*_VALUES, name=_ENUM_NAME, create_type=False),
            nullable=False,
            server_default=sa.text(f"'PUBLISHED'::{_ENUM_NAME}"),
        ),
    )

    # The one row that was using ``enabled`` as a staging switch.
    op.execute("UPDATE skill SET status = 'STAGED' WHERE origin = 'SYSTEM' AND NOT enabled")

    # A CUSTOM skill's toggle was its owner's answer stored on the shared
    # row; move it to where every other origin's answer already lives.
    # Values are carried across as they stand — including the rows that
    # predate uploads landing off, whose owners may never have opted in
    # deliberately. Changing those would change what those users
    # experience, which is a product decision and not a migration's.
    op.execute(
        """
        INSERT INTO user_skill_preference (user_id, skill_id, enabled, created_at, updated_at)
        SELECT user_id, id, enabled, created_at, updated_at
          FROM skill
         WHERE origin = 'CUSTOM'
        ON CONFLICT (user_id, skill_id) DO NOTHING
        """
    )

    # An OFFICIAL skill left switched on was every user's default through
    # ``COALESCE(pref.enabled, skill.enabled)`` — on for everyone who had
    # never touched their toggle. Nothing replaces that after this
    # revision, so the default has to be materialised as the explicit
    # answers it was standing in for, or those users lose the skill with
    # no audit trail and nothing on any surface to explain it. Only rows
    # that were actually on: a default of off was already indistinguishable
    # from silence.
    op.execute(
        """
        INSERT INTO user_skill_preference (user_id, skill_id, enabled, created_at, updated_at)
        SELECT u.id, s.id, true, now(), now()
          FROM skill s
         CROSS JOIN "user" u
         WHERE s.origin = 'OFFICIAL' AND s.enabled
        ON CONFLICT (user_id, skill_id) DO NOTHING
        """
    )

    # SYSTEM skills are agent-only: never listed to a user, never
    # togglable. Rows here are residue from before SYSTEM and OFFICIAL
    # were separate origins — invisible in the UI, unreachable by any
    # write path, and ignored by the new formula. Dropping them makes
    # "a preference row exists only for a skill its user can see" an
    # invariant rather than a description.
    op.execute(
        """
        DELETE FROM user_skill_preference p
         USING skill s
         WHERE s.id = p.skill_id AND s.origin = 'SYSTEM'
        """
    )

    # TRANSITIONAL — hold ``enabled`` at the value that keeps the
    # pre-``status`` worker's ``COALESCE(pref.enabled, skill.enabled)``
    # agreeing with the new formula. SYSTEM has no preference rows, so
    # the old expression reads this column and it must mirror liveness;
    # OFFICIAL and CUSTOM now always have one, so it is never reached.
    # Mirrored in ``app.services.skill_enablement.legacy_enabled``.
    op.execute("UPDATE skill SET enabled = (origin = 'SYSTEM' AND status = 'PUBLISHED')")

    op.execute("ALTER TABLE skill ALTER COLUMN status DROP DEFAULT")


def downgrade() -> None:
    """Schema-safe but **lossy on a round-trip**, in three known ways.

    A preference row carries no record of who wrote it, so the CUSTOM
    delete below cannot tell a backfilled row from one a user had before
    this revision and drops both. The SYSTEM residue deleted on the way
    up is not recreated either: it was unreachable data, and inventing
    which users it belonged to would be worse than leaving it gone.

    The ``OFFICIAL`` rows materialised from a platform default are
    deliberately **left in place**. Deleting them would take the 200-odd
    genuine choices beside them, which are indistinguishable; and under
    the formula this restores they say exactly what the default they
    replaced said, so leaving them preserves behaviour rather than
    corrupting it. The stale ``skill.enabled`` beneath them is what goes
    unrestored.

    None of this loses a *listing*; it loses some users' answers about
    one. Re-running the upgrade afterwards is safe.
    """
    op.execute(
        """
        UPDATE skill s
           SET enabled = COALESCE(p.enabled, false)
          FROM user_skill_preference p
         WHERE p.skill_id = s.id AND p.user_id = s.user_id AND s.origin = 'CUSTOM'
        """
    )
    op.execute(
        """
        DELETE FROM user_skill_preference p
         USING skill s
         WHERE s.id = p.skill_id AND s.origin = 'CUSTOM' 
        """
    )
    op.execute("UPDATE skill SET enabled = (status = 'PUBLISHED') WHERE origin = 'SYSTEM'")

    op.drop_column("skill", "status")
    op.execute(f"DROP TYPE {_ENUM_NAME}")
