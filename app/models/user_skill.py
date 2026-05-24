"""UserSkill — per-user uploaded SKILL.md bundle (ZIP) metadata.

The bundle itself lives in OSS at ``bundle_key``; this row carries
the rendering metadata (``name`` / ``description``) the picker shows
and the per-user enable flag the worker honors at session start.

``name`` is extracted from the bundle's ``SKILL.md`` YAML frontmatter
on registration (``POST /api/skills``); operators can later override
via ``PATCH``. ``(user_id, name)`` is unique — within one user the
name is the skill's identifier (the worker stages the bundle into the
sandbox at ``/workspace/.user_skills/<name>/``).

``enabled=false`` keeps the metadata + bundle around but tells the
worker's per-session staging step to skip — a cheap toggle vs. a
full delete + re-upload.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    String,
    TIMESTAMP,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class UserSkill(Base):
    __tablename__ = "user_skill"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    # ``user_skills/{user_id}/{uuid4}/{filename}.zip``; UNIQUE because
    # every PUT mints a fresh UUID, and the index serves the orphan-
    # cleanup cron's "is this bundle still referenced" lookup.
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
        # Per-user skill names are the identifier the LLM sees; same
        # user can't have two skills with the same name. The composite
        # index this creates also serves ``WHERE user_id = ?`` queries
        # (picker list, per-session staging) and the FK CASCADE — no
        # separate ``user_id`` index needed.
        UniqueConstraint("user_id", "name", name="uq_user_skill_user_id_name"),
    )

    def __repr__(self) -> str:
        return f"<UserSkill(id={self.id}, user_id={self.user_id}, name={self.name!r})>"
