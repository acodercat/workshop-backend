"""recipe_origin: rename BUILTIN to OFFICIAL

A recipe is always user-picked from the composer, so its platform tier
maps to ``OFFICIAL`` (platform-provided, user-selected), not the
always-on ``SYSTEM`` tier skills have. This renames the single platform
origin ``BUILTIN`` → ``OFFICIAL``; ``CUSTOM`` is unchanged. Both remain
platform-owned (``user_id IS NULL``).

Swaps the enum type (matching ``b5e1c9d3a720`` / the skill migration
``e5b9c1a4d7f3``), mapping existing ``BUILTIN`` rows to ``OFFICIAL``.
The origin/user_id consistency check and the platform-name partial
unique index are updated off the old ``builtin`` label. Data-safe and
reversible.

Revision ID: c6e2a9f4b1d7
Revises: e5b9c1a4d7f3
Create Date: 2026-07-06

"""
from typing import Sequence, Union

from alembic import op


revision: str = "c6e2a9f4b1d7"
down_revision: Union[str, Sequence[str], None] = "e5b9c1a4d7f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ENUM = "recipe_origin"
_CHECK = "ck_recipe_origin_user_id_consistent"


def _swap_enum(values: tuple[str, ...], mapping_case: str) -> None:
    """Recreate ``recipe_origin`` with ``values`` and re-cast the column.

    ``mapping_case`` is the ``USING`` expression body mapping each old
    label to a new one; the caller drops/re-adds the check constraint
    around this call since the constraint references ``origin``.
    """
    op.execute(f"ALTER TYPE {_ENUM} RENAME TO {_ENUM}_old")
    op.execute(f"CREATE TYPE {_ENUM} AS ENUM ({', '.join(repr(v) for v in values)})")
    op.execute("ALTER TABLE recipe ALTER COLUMN origin DROP DEFAULT")
    op.execute(
        f"ALTER TABLE recipe ALTER COLUMN origin TYPE {_ENUM} "
        f"USING ({mapping_case})::{_ENUM}"
    )
    op.execute(f"ALTER TABLE recipe ALTER COLUMN origin SET DEFAULT 'CUSTOM'::{_ENUM}")
    op.execute(f"DROP TYPE {_ENUM}_old")


def upgrade() -> None:
    op.execute(f"ALTER TABLE recipe DROP CONSTRAINT {_CHECK}")
    _swap_enum(
        ("OFFICIAL", "CUSTOM"),
        "CASE origin::text WHEN 'BUILTIN' THEN 'OFFICIAL' ELSE origin::text END",
    )
    op.execute(
        f"ALTER TABLE recipe ADD CONSTRAINT {_CHECK} CHECK ("
        "(origin = 'OFFICIAL' AND user_id IS NULL) "
        "OR (origin = 'CUSTOM' AND user_id IS NOT NULL))"
    )
    op.execute("ALTER INDEX uq_recipe_builtin_name RENAME TO uq_recipe_platform_name")


def downgrade() -> None:
    op.execute("ALTER INDEX uq_recipe_platform_name RENAME TO uq_recipe_builtin_name")
    op.execute(f"ALTER TABLE recipe DROP CONSTRAINT {_CHECK}")
    _swap_enum(
        ("BUILTIN", "CUSTOM"),
        "CASE origin::text WHEN 'OFFICIAL' THEN 'BUILTIN' ELSE origin::text END",
    )
    op.execute(
        f"ALTER TABLE recipe ADD CONSTRAINT {_CHECK} CHECK ("
        "(origin = 'BUILTIN' AND user_id IS NULL) "
        "OR (origin = 'CUSTOM' AND user_id IS NOT NULL))"
    )
