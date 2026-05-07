from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


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
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PasswordChange(BaseModel):
    """Body for ``PUT /me/password``.

    ``new_password`` is capped at 72 chars because bcrypt silently
    truncates anything past 72 bytes — keeping the cap explicit means
    the user can't end up in a state where the password they typed
    differs from the prefix actually used at next login."""

    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6, max_length=72)


__all__ = ["UserResponse", "PasswordChange"]
