"""Staff — an operator account for the internal admin console.

Deliberately a **separate table** from ``user`` rather than an
``is_staff`` flag on it:

- Operator identity is not customer identity. Running the console must
  not require holding a customer account (with its credit balance,
  sessions, and referral code), and a customer row must never be one
  UPDATE away from platform-wide privileges.
- Credentials and revocation are independent: staff carry their own
  ``password_hash`` and ``token_version``, so a staff force-logout can't
  disturb a customer session and vice versa.
- The customer ``user`` table is on the hot request path; the console's
  access-control columns don't belong there.

Do **not** reuse ``organization_member.role`` for this. That enum
(``OWNER`` / ``ADMIN`` / ``MEMBER``) is a *tenant*-scoped role describing
a user's standing inside one organization; staff roles here are
*platform*-scoped. Conflating the two would make an org owner
indistinguishable from a platform operator.

Authentication is the ``scope="staff"`` JWT class in :mod:`app.auth` — a
third token class alongside user tokens and share-view tokens, mutually
exclusive with both by construction. It lives beside them rather than in
an admin-only module precisely so that exclusion stays auditable in one
place.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Enum as SAEnum, Integer, String, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class StaffRole(StrEnum):
    """Platform-scoped operator role, ordered least → most privileged.

    Three levels, each with a distinct reason to exist:

    - ``VIEWER``   — read-only. Support staff answering "what happened to
      this user's balance / order" without the ability to change it.
    - ``OPERATOR`` — day-to-day operations: credit adjustments, withdrawal
      review, invite issuance, platform-skill management.
    - ``OWNER``    — everything ``OPERATOR`` can do, plus managing staff
      accounts themselves.

    Finer-grained, per-resource permissions get their own table if and
    when a real need appears; a linear ladder covers the current team and
    keeps every call site a single :meth:`covers` check.
    """

    VIEWER = "VIEWER"
    OPERATOR = "OPERATOR"
    OWNER = "OWNER"

    def covers(self, required: "StaffRole") -> bool:
        """Whether this role satisfies a ``required`` minimum.

        The ladder is linear, so "at least OPERATOR" is a rank
        comparison rather than a set membership test.
        """
        return _ROLE_RANK[self] >= _ROLE_RANK[required]


# Rank lookup backing :meth:`StaffRole.covers`. Kept module-private and
# adjacent to the enum so adding a level is a two-line change that can't
# silently leave a role unranked (a missing key raises on first use).
_ROLE_RANK: dict[StaffRole, int] = {
    StaffRole.VIEWER: 0,
    StaffRole.OPERATOR: 10,
    StaffRole.OWNER: 20,
}


class StaffStatus(StrEnum):
    """Account state. ``DISABLED`` refuses sign-in and invalidates any
    token already issued at its next request — the same posture as
    ``UserStatus``, so deactivating a departing operator is one UPDATE
    and takes effect immediately without a ``token_version`` bump."""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Login identifier. Operator-chosen (not auto-generated like
    # ``user.username``) because staff accounts are provisioned by hand.
    # ``unique=True`` only, same as ``user.username``: the unique
    # constraint's backing index already serves the login lookup, so an
    # extra ``index=True`` would just create a duplicate index.
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Human name shown in the console header and on audit-log rows.
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[StaffRole] = mapped_column(
        SAEnum(
            StaffRole,
            name="staff_role",
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        # Least privilege by default: a row inserted without an explicit
        # role can read the console but cannot change anything.
        server_default=StaffRole.VIEWER.value,
    )
    status: Mapped[StaffStatus] = mapped_column(
        SAEnum(
            StaffStatus,
            name="staff_status",
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        server_default=StaffStatus.ACTIVE.value,
    )
    # Bumped to invalidate every token issued so far — password change,
    # "sign out everywhere", suspected credential leak. Mirrors
    # ``user.token_version``; the staff decoder compares against it.
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )
    # Stamped by the login endpoint. Nullable: an account provisioned but
    # never used has no sensible zero value, and NULL reads as "never".
    last_login_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<Staff(id={self.id}, username={self.username!r}, "
            f"role={self.role.value}, status={self.status.value})>"
        )
