from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.common.email import NormalizedEmail
from app.common.password import BcryptPassword


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


class ReferralResponse(BaseModel):
    """Body for ``GET /me/referral`` — the caller's personal referral
    code plus the reward each successful registration grants them and
    how many have redeemed it so far."""

    code: str
    reward_credits: int
    invited_count: int


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


class SudoProof(BaseModel):
    """Exactly one of the current password or a sudo code from
    ``POST /me/verification-codes``. Checked by
    :func:`app.services.auth.sudo.require_sudo`, which says why the code
    exists."""

    current_password: str | None = Field(default=None, min_length=1)
    verification_code: str | None = Field(default=None, pattern=r"^\d{6}$")

    @model_validator(mode="after")
    def _exactly_one_proof(self) -> "SudoProof":
        if (self.current_password is None) == (self.verification_code is None):
            raise ValueError("Exactly one of 'current_password' or 'verification_code' is required")
        return self


class SudoCodeRequest(BaseModel):
    """Body for ``POST /me/verification-codes`` — send a sudo code to the
    account's own email. ``locale`` picks the email template."""

    locale: str = Field(default="zh-CN", max_length=16)


class PasswordChange(SudoProof):
    """Body for ``PUT /me/password``.

    ``new_password`` is capped at bcrypt's input bound, which is in
    bytes (see :data:`~app.common.password.BCRYPT_MAX_BYTES`)."""

    new_password: BcryptPassword = Field(..., min_length=6)


class EmailChangeCodeRequest(SudoProof):
    """Body for ``POST /me/email/verification-codes`` (step 1 of email
    change): prove identity, then a code goes to the new address.
    ``locale`` controls the language of the email body, mirroring
    registration."""

    new_email: NormalizedEmail
    locale: str = Field(default="zh-CN", max_length=16)


class EmailChangeRequest(BaseModel):
    """Body for ``PUT /me/email`` (step 2 of email change).

    The caller already proved their identity in step 1; here they prove
    possession of the *new* email by quoting back the 6-digit
    verification code that was sent there."""

    new_email: NormalizedEmail
    code: str = Field(..., pattern=r"^\d{6}$")


__all__ = [
    "UserResponse",
    "ProfileUpdate",
    "SudoProof",
    "SudoCodeRequest",
    "PasswordChange",
    "EmailChangeCodeRequest",
    "EmailChangeRequest",
]
