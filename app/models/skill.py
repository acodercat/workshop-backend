"""Skill — uploaded SKILL.md bundle (ZIP) metadata.

Two origins:
- ``BUILTIN`` — platform-shipped, visible to all users, no owner
  (``user_id IS NULL``). Managed by operators / seed scripts.
- ``CUSTOM`` — user-uploaded, scoped to one user (``user_id`` set).
  Managed via ``/api/skills`` endpoints.

The worker stages both into the sandbox at session start and axon's
``use_skill`` tool surfaces the merged set to the LLM.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
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


class SkillOrigin(StrEnum):
    BUILTIN = "BUILTIN"
    CUSTOM = "CUSTOM"


class Skill(Base):
    __tablename__ = "skill"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=True,
    )
    origin: Mapped[SkillOrigin] = mapped_column(
        SAEnum(
            SkillOrigin,
            name="skill_origin",
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
        server_default=text("'CUSTOM'"),
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    bundle_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    bundle_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
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
        # CUSTOM skills: per-user name uniqueness.
        UniqueConstraint("user_id", "name", name="uq_skill_user_id_name"),
        # BUILTIN skills: user_id IS NULL → the composite unique above
        # won't prevent duplicates (NULL ≠ NULL in SQL). This partial
        # index enforces name uniqueness within the BUILTIN set.
        Index(
            "uq_skill_builtin_name", "name",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
        ),
        # ``origin`` and ``user_id`` must agree. Worker visibility uses
        # ``(user_id = $1 OR origin = 'BUILTIN')``, so an orphan row with
        # ``origin='BUILTIN' AND user_id=<someone>`` would be visible to
        # *every* user — letting one customer's CUSTOM bundle leak as
        # the platform default. A reversed mistake
        # (``origin='CUSTOM' AND user_id IS NULL``) is invisible to
        # everyone. Both are nonsense; reject at the DB so neither code
        # nor manual SQL can introduce them.
        CheckConstraint(
            "(origin = 'BUILTIN' AND user_id IS NULL) "
            "OR (origin = 'CUSTOM' AND user_id IS NOT NULL)",
            name="ck_skill_origin_user_id_consistent",
        ),
    )

    def __repr__(self) -> str:
        return f"<Skill(id={self.id}, origin={self.origin}, name={self.name!r})>"
