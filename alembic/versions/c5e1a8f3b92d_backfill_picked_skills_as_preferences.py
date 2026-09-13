"""backfill picked skills as preferences

Turning a skill on now means "offer it in my composer" rather than "put
it in my context every turn", and the composer has started honouring
that: it lists only the skills a user turned on. Until this release it
listed everything visible, so picking a skill never required turning it
on — and most people never did. On this database 92 turns across 12 users
carried a composer pick, and nearly all of those picks were of a skill
the picker had not enabled.

Without this migration those users open the composer to an empty list and
have to rediscover, on the skills page, what they were already using.
So: every ``(user, skill)`` pair the user has actually invoked becomes a
member of that user's picker. 15 rows across 4 users here.

**It overwrites ``enabled = false`` rows, deliberately** —
it finds none on this database, but the rule has to be the same as
metaseq's or the two drift. Under the old model that value meant "keep it
out of my every-turn context", and the UI said as much — *"已关闭：仅能够
通过会话栏选择后调用"*. A user who turned a skill off and went on picking
it was following that instruction, and what they wanted — available to
pick, not riding in context — is exactly what ``true`` means now. Leaving
such rows alone would read their old answer as an answer to a question
nobody asked, and take the skill away from the people using it most
deliberately.

Four conditions each exclude a class of row that a naive rewrite would
choke on or quietly admit:

- ``JOIN skill`` — 21 pairs name a skill that has since been deleted;
  the FK would reject them.
- ``origin <> 'SYSTEM'`` — 20 pairs name a SYSTEM skill, picked back when
  the composer still offered them. ``user_skill_preference`` must have no
  SYSTEM rows: they are agent-only and always on, so a row there would be
  a preference about something nobody can choose.
- ``JOIN "user"`` — ``session.user_id`` is nullable.
- ``status = 'PUBLISHED'`` — a picked listing an operator has since staged
  or retired. No pair on either database is in that state today, but a row
  saying a user wants a skill that reaches nobody is a claim about a
  question they were never asked; if it goes live again they can turn it on
  then, and until then ``GET /skills`` would not show them the switch.

``downgrade`` is a no-op. Once written, these rows are indistinguishable
from ones a user clicked, and they say what the user's own behaviour
already said; deleting them would be the destructive direction.

Revision ID: c5e1a8f3b92d
Revises: b6c1e83f27d4
Create Date: 2026-09-13

"""

from typing import Sequence, Union

from alembic import op

revision: str = "c5e1a8f3b92d"
down_revision: Union[str, Sequence[str], None] = "b6c1e83f27d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Every ``(user_id, skill_id)`` this user has invoked, restricted to rows
# that can legitimately carry a preference. Shared by both steps so they
# cannot disagree about who is in scope.
_PICKED = """
    SELECT DISTINCT se.user_id AS user_id, s.id AS skill_id
      FROM session_turn t
      JOIN session se ON se.id = t.session_id
      JOIN "user" u ON u.id = se.user_id
      CROSS JOIN LATERAL jsonb_array_elements(t.skills) AS e(sk)
      JOIN skill s ON s.id = (e.sk->>'id')::int
     WHERE s.origin <> 'SYSTEM'
       AND s.status = 'PUBLISHED'
"""


def upgrade() -> None:
    # One upsert covers both cases: a user who never said anything gets a
    # row, and one who said no under the old meaning gets it flipped. The
    # ``WHERE`` leaves rows that already say yes entirely alone rather than
    # rewriting their ``updated_at`` to no effect, which also makes a re-run
    # a no-op. ``DISTINCT`` in ``_PICKED`` is load-bearing here: the same
    # pair twice would raise "ON CONFLICT DO UPDATE command cannot affect
    # row a second time".
    op.execute(
        f"""
        INSERT INTO user_skill_preference (user_id, skill_id, enabled, created_at, updated_at)
        SELECT picked.user_id, picked.skill_id, true, now(), now()
          FROM ({_PICKED}) AS picked
        ON CONFLICT (user_id, skill_id) DO UPDATE
           SET enabled = true, updated_at = now()
         WHERE NOT user_skill_preference.enabled
        """
    )


def downgrade() -> None:
    """No-op — see the module docstring."""
