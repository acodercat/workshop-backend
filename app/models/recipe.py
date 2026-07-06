"""Recipe — a user-selectable scenario packaged as a reusable prompt scaffold.

A recipe is picked from the composer (unlike a :class:`Skill`, which the
agent chooses on its own). Its ``instructions`` are injected into the
turn as an axon ``<system-reminder>`` block — the *how* (steering),
kept separate from the user's query — the *what*. The per-turn snapshot
lives on ``session_turn.recipe``; the worker injects ``instructions``
verbatim.

Two origins (mirrors :class:`Skill`; a recipe is always user-picked, so
its platform tier is ``OFFICIAL`` — there is no always-on ``SYSTEM``
recipe the way skills have one):
- ``OFFICIAL`` — platform-shipped, visible to all users, no owner
  (``user_id IS NULL``). Managed by operators / seed scripts.
- ``CUSTOM`` — user-authored, scoped to one user (``user_id`` set).
  Managed via ``/api/recipes`` endpoints.

Unlike a skill bundle (immutable bytes behind an OSS cache key), a
recipe's ``instructions`` are plain text with no staging step, so they
are freely editable in place — see ``RecipeUpdate``.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TIMESTAMP,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class RecipeOrigin(StrEnum):
    OFFICIAL = "OFFICIAL"
    CUSTOM = "CUSTOM"


class Recipe(Base):
    __tablename__ = "recipe"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=True,
    )
    origin: Mapped[RecipeOrigin] = mapped_column(
        SAEnum(
            RecipeOrigin,
            name="recipe_origin",
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
        server_default=text("'CUSTOM'"),
    )
    # Stable internal identifier (kebab for OFFICIAL, opaque for CUSTOM).
    # Drives the uniqueness model; the human title is ``display_name``.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # The system-reminder payload (the HOW), used verbatim — the worker
    # injects it as an axon ``<system-reminder>`` block alongside the
    # user's query.
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        # CUSTOM recipes: per-user name uniqueness.
        UniqueConstraint("user_id", "name", name="uq_recipe_user_id_name"),
        # OFFICIAL recipes: user_id IS NULL → the composite unique above
        # won't prevent duplicates (NULL ≠ NULL in SQL). This partial
        # index enforces name uniqueness within the platform set.
        Index(
            "uq_recipe_platform_name", "name",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
        ),
        # ``origin`` and ``user_id`` must agree — same invariant as
        # ``skill``: an OFFICIAL row with an owner would leak one user's
        # recipe as a platform default; a CUSTOM row without an owner is
        # invisible to everyone. Reject both at the DB.
        CheckConstraint(
            "(origin = 'OFFICIAL' AND user_id IS NULL) "
            "OR (origin = 'CUSTOM' AND user_id IS NOT NULL)",
            name="ck_recipe_origin_user_id_consistent",
        ),
    )

    def __repr__(self) -> str:
        return f"<Recipe(id={self.id}, origin={self.origin}, name={self.name!r})>"
