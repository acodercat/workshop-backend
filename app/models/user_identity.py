"""The link between a local account and the external identity that owns it.

Sign-in is federated: an identity provider (the Meeting service) proves
who someone is, and this row says which local ``user`` that is. Every
business table keeps keying on ``user.id`` — sessions, credits, projects,
the worker's writes — so none of them needs to know a provider exists.

An identity is named by ``(provider, issuer, subject)``:

- ``provider`` — which identity provider vouched for it
  (``"meeting"``, :data:`~app.services.identity.provider.PROVIDER_NAME`).
- ``issuer`` — the origin of the deployment that did. Two deployments of
  the same provider number their accounts independently, so a subject is
  only meaningful beside the issuer that minted it; without this column,
  pointing the backend at a different Meeting host would hand every
  existing account to whoever holds the same number over there.
- ``subject`` — the provider's own stable account id. Never the email:
  that can change at the provider, and an identity that follows it would
  silently become someone else's.

A separate table rather than three columns on ``user`` because the
relation is one-to-many by nature — the day a second provider arrives it
is a row, not a schema change.
"""

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class UserIdentity(Base):
    __tablename__ = "user_identity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # ``CASCADE``: an identity is meaningless without its account, and a
    # leftover row would route the next sign-in to a user that is gone.
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # The federation key, and the arbiter of a first sign-in racing
        # itself: two requests provisioning the same identity collide
        # here and the loser re-reads the winner's row.
        UniqueConstraint("provider", "issuer", "subject", name="uq_user_identity_subject"),
        Index("ix_user_identity_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        return f"<UserIdentity(user_id={self.user_id}, provider='{self.provider}')>"
