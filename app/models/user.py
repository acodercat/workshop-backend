from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base

if TYPE_CHECKING:
    from app.models.user_feedback import UserFeedback


class UserStatus(StrEnum):
    """Lifecycle status of a user account.

    - ``ACTIVE`` — can log in. Default for fresh inserts: a valid
      invite code is the access grant, so registration activates
      immediately.
    - ``DISABLED`` — manually disabled by an admin; login refused.
    - ``SUSPENDED`` — temporarily blocked (e.g. risk control); login refused.

    Only ``ACTIVE`` is allowed past :func:`app.auth.is_user_active`. Member
    values match the Postgres ``user_status`` enum and the Python
    member names — same convention as :class:`SessionStatus`.
    """

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    SUSPENDED = "SUSPENDED"


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Display name shown in the UI. Distinct from ``username`` because
    # the user can change their display name freely; ``username`` is
    # the stable login identifier.
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Stable login identifier. ``UNIQUE`` enforced; case-sensitive
    # storage but the registration validator caps to alphanumeric +
    # ``_``/``-`` so casing collisions are unlikely in practice.
    username: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    # Used for verification email + future contact. ``NULL`` for legacy
    # users created before the email-verification flow existed; new
    # registrations always populate it. ``12dc6d69a9ad`` made it unique through a
    # partial index (non-NULL rows only); ``b1c7e29f4d8a`` replaced that with the
    # plain ``user_email_key`` constraint this declaration renders, which allows
    # multiple NULLs anyway (Postgres treats them as distinct).
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    # No ``phone``: the channel was removed on this branch and the column went
    # with it in ``b7e2d9a4c1f5``. Master still has both — a merge that brings
    # the field back is bringing back a column this database does not have, and
    # ``e5c1a9b7d2f4`` (its partial unique index) is not in the graph to
    # recreate it.
    # Avatar variant counter. ``0`` is the default look derived from
    # ``id`` alone; each "shuffle" click in the settings UI bumps it
    # by 1, picking a different deterministic variant. The renderer
    # is pure client-side (``boring-avatars`` over ``id-r{revision}``
    # as the seed string), so this column never holds image bytes /
    # URLs — only the variant index. Using ``int`` instead of a
    # free-form seed string means we can't trip a length cap no
    # matter how many times the user shuffles.
    avatar_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Default ``ACTIVE``: registration is invite-gated, and a valid
    # invite is itself the access grant, so a freshly inserted row can
    # log in immediately. ``DISABLED`` / ``SUSPENDED`` are set later by
    # admin / risk-control tooling.
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(
            UserStatus,
            name="user_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=UserStatus.ACTIVE,
        server_default=UserStatus.ACTIVE.value,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # The user's current default organization (mirrors OpenAI's
    # ``default_organization``). Set to the user's personal org at
    # registration. The column is nullable purely because of the
    # circular FK at row-creation time —
    # :class:`~app.models.organization.Organization` requires a valid
    # ``owner_user_id``, so the user has to exist first; we then
    # create the org and UPDATE this column inside the same
    # transaction. Every production code path treats it as NOT NULL
    # after registration completes.
    # ``ON DELETE RESTRICT`` keeps the cleanup of an org with linked
    # users a deliberate ops step rather than a silent cascade.
    organization_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("organization.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # PAYG credit balance, in **µ-credits** (1 credit = 10_000_000 µ-credits;
    # 1 credit ≈ $0.01 USD of LLM cost). Cached materialization of
    # ``SUM(credit_ledger.amount WHERE user_id = self.id)``; the ledger
    # is the source of truth. ``bigint`` keeps all arithmetic integer
    # (the alternative ``numeric`` accumulates float-style errors over
    # millions of rows). All writes go through
    # :mod:`app.services.credits._internal` so the invariant holds.
    credit_balance: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # App-level feedback the user submitted to the admin team.
    # Default cascade (``save-update, merge``) is correct here — the
    # FK's ``ON DELETE SET NULL`` preserves feedback rows for audit
    # when a user is deleted, so we deliberately don't add
    # ``delete-orphan``.
    feedback_submissions: Mapped[list["UserFeedback"]] = relationship(
        back_populates="user",
    )

    def __repr__(self):
        return f"<User(id={self.id}, name='{self.name}', status='{self.status.value}')>"
