from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class UserResponse(BaseModel):
    """Public-facing user shape — what ``GET /me`` and registration
    return. ``status`` is intentionally omitted: a logged-in caller is
    by definition ``ACTIVE``, and exposing the field would just leak
    an enum to the frontend that has no use for it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    name: str
    email: str | None
    avatar_revision: int
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ProfileUpdate(BaseModel):
    """Body for ``PATCH /me``.

    All fields optional — the endpoint applies a partial update via
    ``model_dump(exclude_unset=True)`` so the caller can change just
    one. ``avatar_revision`` is non-negative; ``0`` is the default
    look. ``username`` is intentionally *not* part of the editable
    surface: it's the JWT ``sub`` and the auto-generated identifier
    (``axon_${id}``). ``email`` and the password are absent for a
    different reason — the identity provider owns them, and they are
    changed there."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    avatar_revision: int | None = Field(default=None, ge=0)


__all__ = [
    "UserResponse",
    "ProfileUpdate",
]
