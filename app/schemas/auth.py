from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.common.email import NormalizedEmail
from app.schemas.user import UserResponse

#
# Accounts live at the identity provider (see ``app/services/identity``),
# so these bodies are requests *to be relayed*, and validate only what any
# request must: that the email is an email, that the code is six digits,
# that nothing is unboundedly long. The provider's own rules — how long a
# password may be, which invite codes exist — belong to its adapter
# (``identity/provider.py``), the one place that speaks for it.

# Generous on purpose: an upper bound on what is accepted off the wire,
# not a password policy.
_MAX_PASSWORD_LENGTH = 128


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str


class _IdentifierBody(BaseModel):
    """The email that identifies the account at the provider."""

    email: NormalizedEmail


class VerificationCodeRequest(_IdentifierBody):
    """Body for ``POST /auth/verification-codes`` — have the provider send
    a verification code to ``email``.

    ``purpose`` says which request the code will prove: the provider
    issues a different kind for each and will not accept one for the
    other. ``invite_code`` is forwarded when registering with one."""

    purpose: Literal["register", "recover"] = "register"
    invite_code: str | None = Field(default=None, min_length=1, max_length=64)


class RegisterRequest(_IdentifierBody):
    """Body for ``POST /users`` — create an account at the provider and
    sign straight in to it."""

    password: str = Field(..., min_length=1, max_length=_MAX_PASSWORD_LENGTH)
    code: str = Field(..., pattern=r"^\d{6}$")
    name: str = Field(..., min_length=1, max_length=255)
    invite_code: str | None = Field(default=None, min_length=1, max_length=64)


class PasswordResetRequest(_IdentifierBody):
    """Body for ``POST /auth/password-resets`` — set a new password, proven
    by a ``recover`` verification code."""

    password: str = Field(..., min_length=1, max_length=_MAX_PASSWORD_LENGTH)
    code: str = Field(..., pattern=r"^\d{6}$")


class LoggedInResponse(BaseModel):
    """What registration answers: the new account, already signed in."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["logged_in"] = "logged_in"
    access_token: str
    token_type: str
    user: UserResponse
