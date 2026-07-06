"""skill_origin: replace BUILTIN with SYSTEM + OFFICIAL

The single platform origin ``BUILTIN`` splits into two, both still
platform-owned (``user_id IS NULL``):

- ``SYSTEM``   — always-on core skills (the direct successor of BUILTIN)
- ``OFFICIAL`` — platform-published skills a user opts into

``CUSTOM`` is unchanged. Postgres has no ``ALTER TYPE ... RENAME/ADD
VALUE`` that stays transaction-safe when the new value is referenced in
the same migration, so we swap the enum type (matching
``b5e1c9d3a720``'s idiom), mapping existing ``BUILTIN`` rows to
``SYSTEM``. The origin/user_id consistency check widens to accept both
platform origins, and the platform-name partial unique index is renamed
off the old ``builtin`` label.

Data-safe and reversible: downgrade collapses SYSTEM/OFFICIAL back to
BUILTIN (both are ``user_id IS NULL``, so the legacy constraint holds).

Revision ID: e5b9c1a4d7f3
Revises: a9d3e7f5c2b1
Create Date: 2026-07-06

"""
from typing import Sequence, Union

from alembic import op


revision: str = "e5b9c1a4d7f3"
down_revision: Union[str, Sequence[str], None] = "a9d3e7f5c2b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ENUM = "skill_origin"
_CHECK = "ck_skill_origin_user_id_consistent"


def _swap_enum(values: tuple[str, ...], mapping_case: str) -> None:
    """Recreate ``skill_origin`` with ``values`` and re-cast the column.

    ``mapping_case`` is the ``USING`` expression body that maps each old
    label to a new one; the caller drops/re-adds the check constraint
    around this call since the constraint references ``origin``.
    """
    op.execute(f"ALTER TYPE {_ENUM} RENAME TO {_ENUM}_old")
    op.execute(f"CREATE TYPE {_ENUM} AS ENUM ({', '.join(repr(v) for v in values)})")
    op.execute("ALTER TABLE skill ALTER COLUMN origin DROP DEFAULT")
    op.execute(
        f"ALTER TABLE skill ALTER COLUMN origin TYPE {_ENUM} "
        f"USING ({mapping_case})::{_ENUM}"
    )
    op.execute(f"ALTER TABLE skill ALTER COLUMN origin SET DEFAULT 'CUSTOM'::{_ENUM}")
    op.execute(f"DROP TYPE {_ENUM}_old")


def upgrade() -> None:
    op.execute(f"ALTER TABLE skill DROP CONSTRAINT {_CHECK}")
    _swap_enum(
        ("SYSTEM", "OFFICIAL", "CUSTOM"),
        "CASE origin::text WHEN 'BUILTIN' THEN 'SYSTEM' ELSE origin::text END",
    )
    op.execute(
        f"ALTER TABLE skill ADD CONSTRAINT {_CHECK} CHECK ("
        "(origin IN ('SYSTEM', 'OFFICIAL') AND user_id IS NULL) "
        "OR (origin = 'CUSTOM' AND user_id IS NOT NULL))"
    )
    op.execute("ALTER INDEX uq_skill_builtin_name RENAME TO uq_skill_platform_name")


def downgrade() -> None:
    op.execute("ALTER INDEX uq_skill_platform_name RENAME TO uq_skill_builtin_name")
    op.execute(f"ALTER TABLE skill DROP CONSTRAINT {_CHECK}")
    _swap_enum(
        ("BUILTIN", "CUSTOM"),
        "CASE origin::text WHEN 'CUSTOM' THEN 'CUSTOM' ELSE 'BUILTIN' END",
    )
    op.execute(
        f"ALTER TABLE skill ADD CONSTRAINT {_CHECK} CHECK ("
        "(origin = 'BUILTIN' AND user_id IS NULL) "
        "OR (origin = 'CUSTOM' AND user_id IS NOT NULL))"
    )
