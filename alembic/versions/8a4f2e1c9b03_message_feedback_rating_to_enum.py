"""message_feedback rating: SmallInteger(1/-1) → enum(UP/DOWN)

Aligns with the rest of the schema (``MessageRole``, ``SessionStatus``)
where binary / categorical states live as Postgres native enums named
in snake_case. Removes the ``parse_rating`` / ``serialize_rating``
adapters in :mod:`app.schemas.message_feedback` since the wire shape
(``"UP" | "DOWN"``) and DB shape are now identical.

Conversion uses an ``ALTER COLUMN ... USING`` cast: ``1 → 'UP'``,
``-1 → 'DOWN'``. Pre-existing rows are migrated in-place; the old
``ck_message_feedback_rating`` CHECK is dropped because the enum
constraint subsumes it.

Revision ID: 8a4f2e1c9b03
Revises: 63d1d0ff7186
Create Date: 2026-05-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8a4f2e1c9b03"
down_revision: Union[str, Sequence[str], None] = "63d1d0ff7186"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_RATING_ENUM_NAME = "message_feedback_rating"


def upgrade() -> None:
    """Upgrade schema."""
    # Drop the int-shaped check first; the new enum will replace it.
    op.drop_constraint(
        "ck_message_feedback_rating",
        "message_feedback",
        type_="check",
    )

    rating_enum = sa.Enum("UP", "DOWN", name=_RATING_ENUM_NAME)
    rating_enum.create(op.get_bind(), checkfirst=False)

    op.alter_column(
        "message_feedback",
        "rating",
        type_=rating_enum,
        existing_type=sa.SmallInteger(),
        existing_nullable=False,
        postgresql_using=(
            "CASE WHEN rating = 1 THEN 'UP' ELSE 'DOWN' END::"
            f"{_RATING_ENUM_NAME}"
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        "message_feedback",
        "rating",
        type_=sa.SmallInteger(),
        existing_nullable=False,
        postgresql_using=(
            "CASE WHEN rating = 'UP' THEN 1 ELSE -1 END"
        ),
    )

    op.create_check_constraint(
        "ck_message_feedback_rating",
        "message_feedback",
        "rating IN (1, -1)",
    )

    sa.Enum(name=_RATING_ENUM_NAME).drop(op.get_bind(), checkfirst=False)
