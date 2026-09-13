"""Whether one user wants one skill in their every-turn context.

One row per ``(user_id, skill_id)``, written by
:func:`app.api.public.skill.update_skill` whatever the skill's origin.
An ``OFFICIAL`` row is shared by everyone and a ``CUSTOM`` one has an
audience of one, but the answer is the user's either way, so it lives
here either way — the ``skill`` row carries no opinion about it.

**No row is the answer "never said", and that reads as off.** There is
no platform-wide default to fall back to: a skill nobody asked for does
not get to spend a user's context budget, and an operator who wants one
noticed puts it somewhere users see it rather than somewhere they do
not. ``skill.status`` still gates everything on top of this: a listing
that is not live reaches nobody, however this row reads.

``SYSTEM`` skills have no rows here and never should: they are
agent-only, never listed to a user and not togglable, so there is
nothing for a user to have said.
"""

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    ForeignKey,
    Index,
    Integer,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class UserSkillPreference(Base):
    __tablename__ = "user_skill_preference"

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    skill_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("skill.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # The composite PK leads with ``user_id``, so it can't serve the
        # ``ON DELETE CASCADE`` from ``skill`` (deleting a CUSTOM skill is
        # a real API path). Postgres doesn't auto-index FK columns —
        # without this the cascade seq-scans the table.
        Index("ix_user_skill_preference_skill_id", "skill_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<UserSkillPreference(user_id={self.user_id}, "
            f"skill_id={self.skill_id}, enabled={self.enabled})>"
        )
