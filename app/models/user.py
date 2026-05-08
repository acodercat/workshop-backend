from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Enum as SAEnum,
    Integer,
    String,
    TIMESTAMP,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from app.database import Base

if TYPE_CHECKING:
    from app.models.user_feedback import UserFeedback


class UserStatus(StrEnum):
    """Lifecycle status of a user account.

    - ``ACTIVE`` — can log in.
    - ``DISABLED`` — manually disabled by an admin; login refused.
    - ``SUSPENDED`` — temporarily blocked (e.g. risk control); login refused.
    - ``PENDING`` — created but not yet approved; login refused. Default
      for fresh inserts so a new account is opt-in to access.

    Only ``ACTIVE`` is allowed past :func:`authenticate_user`. Member
    values match the Postgres ``user_status`` enum and the Python
    member names — same convention as :class:`SessionStatus`.
    """

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    SUSPENDED = "SUSPENDED"
    PENDING = "PENDING"


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
    # registrations always populate it. ``UNIQUE`` is partial (only
    # enforced on non-NULL rows) — see migration ``12dc6d69a9ad``.
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
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
    # Default ``PENDING`` so a freshly inserted row is locked out by
    # default; promotion to ``ACTIVE`` is an explicit step (admin tool /
    # seed script). Existing rows were backfilled to ``ACTIVE`` in the
    # migration that introduced this column.
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(
            UserStatus,
            name="user_status",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=UserStatus.PENDING,
        server_default=UserStatus.PENDING.value,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP,
        server_default=func.now(),
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
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
