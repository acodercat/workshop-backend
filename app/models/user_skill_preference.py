"""Per-user override of a skill's default ``enabled`` flag.

CUSTOM skills' ``enabled`` lives on the ``skill`` row itself — the
owner can flip it directly. BUILTIN skills are platform-wide rows
(``user_id IS NULL``) that no end user can mutate without affecting
everyone, so per-user toggles need a separate place to live.

This table fills that gap. One row per ``(user_id, skill_id)`` —
``enabled`` overrides ``skill.enabled`` when present; absent rows fall
back to the skill's default. Mechanically the override works for both
origins, but the only path that writes to it today is the BUILTIN
toggle in :func:`app.api.endpoints.skill.update_skill`.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    TIMESTAMP,
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
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<UserSkillPreference(user_id={self.user_id}, "
            f"skill_id={self.skill_id}, enabled={self.enabled})>"
        )
