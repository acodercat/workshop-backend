"""Pydantic schemas for the session-share feature.

Two surfaces:

- **Owner-side** (``ShareCreate`` / ``ShareResponse``) — payloads the
  logged-in session owner sees when managing share links.
- **Public-side** (``ShareDetailResponse`` / ``ShareAccessTokenRequest`` /
  ``ShareAccessTokenResponse``) — what the share recipient sees.
  Deliberately omits ``user_id`` / ``model_id`` / any other internal
  field that doesn't belong in a public read.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.session import SessionStatus
from app.schemas.session import MessageResponse


class ShareCreate(BaseModel):
    """Body for ``POST /sessions/{id}/share``.

    ``password`` is optional; when absent, the share is public (anyone
    with the URL can view). Capped at 8 chars to match product
    requirements — share passwords are short, throwaway codes the
    creator dictates verbally / by chat, not credentials. Empty string
    is rejected (use ``null`` to mean "no password").
    """

    password: str | None = Field(default=None, min_length=1, max_length=8)


class ShareResponse(BaseModel):
    """Owner-side view of a share row.

    ``has_password`` is the projection of ``password_hash IS NOT NULL`` —
    the actual hash never leaves the DB.
    """

    id: int
    token: str
    has_password: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ShareDetailResponse(BaseModel):
    """Public-side payload returned by ``GET /shares/{token}`` when
    the recipient has the right credentials. Mirrors the field set of
    :class:`SessionDetailResponse` minus the owner-only fields."""

    id: UUID
    title: str | None
    status: SessionStatus
    created_at: datetime
    messages: list[MessageResponse]


class ShareAccessTokenRequest(BaseModel):
    """Body for ``POST /shares/{token}/access-tokens``."""

    password: str = Field(..., min_length=1, max_length=128)


class ShareAccessTokenResponse(BaseModel):
    """A short-lived JWT scoped to a single share. Frontend stores it
    in ``sessionStorage`` and sends it as ``Authorization: Bearer ...``
    on subsequent reads. ``expires_at`` is informational so the UI can
    pre-emptively refresh / re-prompt before a request fails."""

    access_token: str
    expires_at: datetime
