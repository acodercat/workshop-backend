"""Organization — billing + ACL boundary.

Mirrors the OpenAI / Anthropic "Organization" concept. Every user
belongs to at least one organization: at registration we auto-create
their personal one (``kind = PERSONAL``), and any future B2B
features (teams with multiple members, invitations, SSO, shared
balance) layer on top of this same shape.

For the foreseeable future the only non-trivial use is the
``PERSONAL`` kind — one user, one org. The ``TEAM`` enum value is
here so the V4 multi-member B2B work drops in without a schema
migration. Same for the role enum: ``ADMIN`` and ``MEMBER`` are
unused today, ``OWNER`` is the only role assigned (to the user
whose personal org this is).

The two tables — :class:`Organization` and :class:`OrganizationMember`
— intentionally stay narrow. Avatars, billing addresses, plan tiers,
SSO config, audit retention etc. arrive in their own tables when
they're actually needed. This module is the **identity layer**, not
the billing layer; the credit ledger continues to attach to
``user_id`` until a future migration moves billing to the org.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class OrganizationKind(StrEnum):
    """Categorises the org so business code can branch on it without
    a separate flag column.

    - ``PERSONAL`` — auto-created at user registration; exactly one
      member (the owner). The default org every individual user
      operates under.
    - ``TEAM`` — multi-member org for B2B. No production code path
      writes this value yet; reserved for V4.
    """

    PERSONAL = "PERSONAL"
    TEAM = "TEAM"


class OrganizationMemberRole(StrEnum):
    """Role of a user inside one organization. Today only ``OWNER`` is
    assigned (to the personal-org owner). ``ADMIN`` / ``MEMBER`` are
    reserved for B2B invitation flows."""

    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


class Organization(Base):
    """One workspace / billing boundary.

    ``owner_user_id`` is the user who created the org. For
    ``PERSONAL`` orgs this is the sole member; for ``TEAM`` orgs
    it's the founding member, with no special billing meaning
    beyond audit ("who originally created this team"). ``ON DELETE
    RESTRICT`` on the FK because deleting a user with owned orgs is
    a manual ops step — automatic cascade would silently drop billing
    history.
    """

    __tablename__ = "organization"

    # Org cardinality is user-scoped — one auto-created personal org
    # per user plus a handful of future team orgs — so it stays well
    # inside the 2.1B Integer ceiling and matches the ``user.id`` /
    # ``owner_user_id`` type. (High-volume append tables — ledger,
    # session_turn, reservation — use BigInteger; identity tables don't.)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[OrganizationKind] = mapped_column(
        SAEnum(
            OrganizationKind,
            name="organization_kind",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    owner_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # "Find this user's owned orgs" — used by the registration
        # backfill and any future "list my organizations" surface.
        Index("ix_organization_owner_user_id", "owner_user_id"),
    )

    def __repr__(self) -> str:
        return f"<Organization(id={self.id}, name={self.name!r}, kind={self.kind.value})>"


class OrganizationMember(Base):
    """User ↔ Organization membership with a role.

    ``UNIQUE(organization_id, user_id)`` makes "one user, one
    membership per org" a DB-enforced invariant. Both FKs cascade
    on parent delete — when an org is deleted, its membership rows
    go with it; when a user is deleted (rare ops action), their
    memberships go too.
    """

    __tablename__ = "organization_member"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[OrganizationMemberRole] = mapped_column(
        SAEnum(
            OrganizationMemberRole,
            name="organization_member_role",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            name="uq_organization_member",
        ),
        # FK index for "list this user's orgs" — Postgres doesn't
        # auto-index FK columns, and we'll query by user_id from the
        # "switch active org" UI.
        Index("ix_organization_member_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<OrganizationMember(org={self.organization_id}, "
            f"user={self.user_id}, role={self.role.value})>"
        )
