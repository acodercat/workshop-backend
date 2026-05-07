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
    (``axon_${id}``) at registration time. Re-introduce here only
    when the frontend grows a flow that handles the cross-device
    sign-out semantics."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    avatar_revision: int | None = Field(default=None, ge=0)


class PasswordChange(BaseModel):
    """Body for ``PUT /me/password``.

    ``new_password`` is capped at 72 chars because bcrypt silently
    truncates anything past 72 bytes — keeping the cap explicit means
    the user can't end up in a state where the password they typed
    differs from the prefix actually used at next login."""

    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6, max_length=72)


class EmailChangeCodeRequest(BaseModel):
    """Body for ``POST /me/email/verification-codes`` (step 1 of email
    change).

    ``current_password`` is the sudo gate — even though the caller is
    already JWT-authenticated, changing the email touches the password-
    recovery surface, so we re-verify possession of the credential
    before sending a code to the new address. ``locale`` controls the
    language of the email body, mirroring registration."""

    new_email: EmailStr
    current_password: str = Field(..., min_length=1)
    locale: str = Field(default="zh-CN", max_length=16)


class EmailChangeRequest(BaseModel):
    """Body for ``PUT /me/email`` (step 2 of email change).

    The caller already proved possession of ``current_password`` in
    step 1; here they prove possession of the *new* email by quoting
    back the 6-digit verification code that was sent there."""

    new_email: EmailStr
    code: str = Field(..., pattern=r"^\d{6}$")


__all__ = [
    "UserResponse",
    "ProfileUpdate",
    "PasswordChange",
    "EmailChangeCodeRequest",
    "EmailChangeRequest",
]
