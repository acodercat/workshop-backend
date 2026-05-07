from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Enum as SAEnum,
    Integer,
    String,
    TIMESTAMP,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from app.database import Base


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
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
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

    def __repr__(self):
        return f"<User(id={self.id}, name='{self.name}', status='{self.status.value}')>"
