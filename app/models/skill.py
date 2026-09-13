"""Skill — uploaded SKILL.md bundle (ZIP) metadata.

Three origins:
- ``SYSTEM`` — system-level, always-on skills for the agent, no owner
  (``user_id IS NULL``). **Hidden from users**: never returned by the
  user-facing ``/api/skills`` endpoints. Managed by operators.
- ``OFFICIAL`` — platform-published skills a user browses and opts into,
  no owner (``user_id IS NULL``). Managed by operators / seed scripts.
- ``CUSTOM`` — user-uploaded, scoped to one user (``user_id`` set).
  Managed via ``/api/skills`` endpoints.

User-facing visibility is ``OFFICIAL`` + the caller's own ``CUSTOM``;
``SYSTEM`` is agent-only. ``SYSTEM`` and ``OFFICIAL`` are both
platform-owned (``user_id IS NULL``). The worker stages all three into
the sandbox so axon's ``use_skill`` tool can surface them to the LLM.

Whether a skill actually reaches a given user is two independent
questions, answered in two places: :class:`SkillStatus` on this row says
whether the listing is live at all, and ``user_skill_preference`` says
whether that user wants it in their context. Neither column means
something different depending on ``origin`` — the one exemption is that
``SYSTEM``, being agent-only, has no user side to consult, which only
the worker ever has to account for since nothing user-facing lists one.
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class SkillOrigin(StrEnum):
    SYSTEM = "SYSTEM"
    OFFICIAL = "OFFICIAL"
    CUSTOM = "CUSTOM"


class SkillStatus(StrEnum):
    """Where the listing sits in its life, independent of any user.

    - ``STAGED`` — uploaded and validated, exposed to nobody. Where an
      operator parks a platform bundle before it goes live.
    - ``PUBLISHED`` — live. The only state a skill can be reached in, by
      ambient discovery and by an explicit composer pick alike.
    - ``RETIRED`` — pulled. The row survives (session snapshots point at
      its id and bundle key) but every route to it is closed, including
      for the users who had turned it on.

    Only ``PUBLISHED`` is reachable; the other two differ in what they
    say about a row, not in what they do to it, and an operator moves
    between them freely. A ``CUSTOM`` skill is ``PUBLISHED`` from birth
    and stays there — it has no review to wait for and no audience to be
    pulled from, and its owner removes it by deleting it.

    Orthogonal to ``user_skill_preference``, which answers a different
    question: not *is this listing live* but *does this user want it in
    their context*. Member values match the Postgres ``skill_status``
    enum and the Python member names — same convention as
    :class:`SkillOrigin`.
    """

    STAGED = "STAGED"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


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
    # Canonical display name + the API fallback (CUSTOM skills, and OFFICIAL
    # skills with no translation for the request locale).
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Optional per-locale overrides ``{locale: name}``, populated when the
    # bundle's SKILL.md ships a ``metadata.display_name`` map (the platform
    # catalog's localization path; a CUSTOM bundle may carry one too). NULL
    # otherwise. The API resolves it against the request locale (see
    # ``skill_i18n``); a PATCH rename clears it so explicit edits win.
    # ``none_as_null`` so a Python ``None`` persists as SQL ``NULL`` (the
    # "no map" state) instead of a JSONB ``'null'`` literal, keeping
    # ``IS NULL`` filters honest.
    display_name_i18n: Mapped[dict[str, str] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    bundle_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    bundle_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Deliberately no default, in either layer. A new ``CUSTOM`` row is
    # ``PUBLISHED`` (its owner can use it at once) and a new platform row
    # is ``STAGED`` (an operator decides when it goes live), so there is
    # no one value a forgotten write should fall back to — and the column
    # this one replaces is the cautionary tale: its ``server_default`` of
    # ``true`` was the opposite of what uploads wanted, which is why the
    # insert had to spell out the opposite.
    status: Mapped[SkillStatus] = mapped_column(
        SAEnum(
            SkillStatus, name="skill_status",
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
    )
    # TRANSITIONAL — dropped in the follow-up migration, do not read.
    # ``core/agent-worker`` is shared by this deployment and metaseq's,
    # and queries ``COALESCE(p.enabled, s.enabled)``; it cannot learn
    # about ``status`` until both databases have the column. Until that
    # worker ships, every write keeps this column at the value that makes
    # the old formula agree with the new one — see ``skill_enablement``.
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
        # Platform skills (SYSTEM/OFFICIAL): user_id IS NULL → the
        # composite unique above won't prevent duplicates (NULL ≠ NULL in
        # SQL). This partial index enforces name uniqueness across the
        # whole platform-owned set.
        Index(
            "uq_skill_platform_name", "name",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
        ),
        # ``origin`` and ``user_id`` must agree. Worker/API visibility
        # uses ``(user_id = $1 OR origin IN ('SYSTEM', 'OFFICIAL'))``, so
        # an orphan row with a platform origin AND an owner would be
        # visible to *every* user — letting one customer's CUSTOM bundle
        # leak as a platform default. The reverse (``origin='CUSTOM' AND
        # user_id IS NULL``) is invisible to everyone. Both are nonsense;
        # reject at the DB so neither code nor manual SQL can introduce them.
        CheckConstraint(
            "(origin IN ('SYSTEM', 'OFFICIAL') AND user_id IS NULL) "
            "OR (origin = 'CUSTOM' AND user_id IS NOT NULL)",
            name="ck_skill_origin_user_id_consistent",
        ),
    )

    def __repr__(self) -> str:
        return f"<Skill(id={self.id}, origin={self.origin}, name={self.name!r})>"
