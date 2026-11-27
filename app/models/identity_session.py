"""One signed-in session: a local access token and the provider session behind it.

The access token is a JWT, but it is not self-sufficient — it carries this
row's id as ``sid`` and is only as good as the row. That is what lets a
sign-out, a revocation at the provider, or an expiry end a session before
the token's own ``exp``.

The row also holds what the backend needs to act as the user at the
provider (``credential``), encrypted at rest. Only
:mod:`app.services.identity.sessions` unseals it, and it never leaves the
backend: the browser holds the JWT and nothing else.

Two timestamps track how fresh the provider's word is, and they mean
different things:

- ``checked_at`` — the last time re-verification was *attempted*. Claiming
  an attempt bumps it first, so concurrent requests on one session make a
  single call to the provider instead of one each.
- ``verified_at`` — the last time the provider *confirmed* the session.
  It only moves on success, so it measures how long the session has run
  on trust while the provider was unreachable.

Collapsing them would make an outage indistinguishable from a
confirmation: either every request retries the provider (and waits out
its timeout), or a session nobody has vouched for lives until it expires.
"""

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class IdentitySession(Base):
    __tablename__ = "identity_session"

    # Random, not sequential: the id travels in the access token, and a
    # guessable one would name other people's sessions.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    identity_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user_identity.id", ondelete="CASCADE"), nullable=False
    )
    # Fernet ciphertext of the provider's credential (see
    # :mod:`app.services.identity.credentials`). Opaque here on purpose:
    # its shape belongs to the provider that issued it.
    credential: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    checked_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    verified_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    # Set once, never cleared. Revoked and expired rows are swept at the
    # user's next sign-in (``sessions.open_session``).
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (Index("ix_identity_session_user_id", "user_id"),)

    def __repr__(self) -> str:
        return f"<IdentitySession(id={self.id}, user_id={self.user_id})>"
