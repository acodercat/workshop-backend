from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.common.email import NormalizedEmail
from app.common.phone import NormalizedPhone
from app.schemas.user import UserResponse


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str


#
# Code flows (registration + login) take **exactly one** of ``email`` /
# ``phone`` as the identifier; the channel decides delivery (DirectMail
# vs Aliyun SMS) and lookup column. The ``axon_${id}`` username and a
# random password placeholder are generated server-side. ``locale`` on
# the code-request payload picks the email template (ignored for SMS).


class _IdentifierBody(BaseModel):
    """Exactly one of ``email`` / ``phone`` identifies the account.
    Exposes ``(channel, identifier)`` for the service layer."""

    email: NormalizedEmail | None = None
    phone: NormalizedPhone | None = None

    @model_validator(mode="after")
    def _exactly_one_identifier(self) -> "_IdentifierBody":
        if bool(self.email) == bool(self.phone):
            raise ValueError("Exactly one of 'email' or 'phone' is required")
        return self

    @property
    def channel(self) -> Literal["email", "phone"]:
        return "phone" if self.phone else "email"

    @property
    def identifier(self) -> str:
        return self.phone or self.email  # type: ignore[return-value]


class RegisterCodeRequest(_IdentifierBody):
    """Body for ``POST /auth/verification-codes`` — send a verification
    code to ``email`` (DirectMail) or ``phone`` (SMS)."""

    locale: Literal["zh-CN", "en-US"] = "zh-CN"


class CodeLoginRequest(_IdentifierBody):
    """Body for ``POST /auth/access-tokens-by-code`` — **login only**.
    Peek-verifies the code and issues a JWT for an existing ``ACTIVE``
    user; an unregistered identifier raises ``not_registered``."""

    code: str = Field(..., pattern=r"^\d{6}$")


class RegisterRequest(_IdentifierBody):
    """Body for ``POST /users`` — invite-gated registration via the code
    flow. A valid ``invite_code`` activates the account immediately, so
    the endpoint logs the user straight in."""

    code: str = Field(..., pattern=r"^\d{6}$")
    name: str = Field(..., min_length=1, max_length=255)
    invite_code: str = Field(..., min_length=1, max_length=32)


class LoggedInResponse(BaseModel):
    """The shared logged-in response for both code login
    (``POST /auth/access-tokens-by-code``) and invite-gated registration
    (``POST /users``) — a JWT is issued and the frontend logs the user
    straight in."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["logged_in"] = "logged_in"
    access_token: str
    token_type: str
    user: UserResponse
