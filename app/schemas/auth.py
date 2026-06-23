from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.common.email import NormalizedEmail
from app.schemas.user import UserResponse


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str


#
# Email-OTP flow: ``email`` + ``code`` + ``name`` is all the user
# supplies. The ``axon_${id}`` username and a random password
# placeholder are generated server-side. ``locale`` on the code-
# request payload controls which template the email renders in;
# frontend passes its current i18n locale, falling back to ``zh-CN``.


class RegisterCodeRequest(BaseModel):
    """Body for ``POST /auth/verification-codes`` — request a
    verification code be sent to ``email``."""

    email: NormalizedEmail
    locale: Literal["zh-CN", "en-US"] = "zh-CN"


class RegisterRequest(BaseModel):
    """Body for ``POST /users`` — invite-gated registration via the
    email-OTP flow. A valid ``invite_code`` activates the account
    immediately, so the endpoint logs the user straight in."""

    email: NormalizedEmail
    code: str = Field(..., pattern=r"^\d{6}$")
    name: str = Field(..., min_length=1, max_length=255)
    invite_code: str = Field(..., min_length=1, max_length=32)


#
# ``POST /auth/access-tokens-by-code`` is **login only** — it
# peek-verifies the code and issues a JWT for an existing ``ACTIVE``
# user. An unregistered email raises ``not_registered``; registration
# is the separate invite-gated ``POST /users``.


class EmailLoginRequest(BaseModel):
    """Body for ``POST /auth/access-tokens-by-code``."""

    email: NormalizedEmail
    code: str = Field(..., pattern=r"^\d{6}$")


class LoggedInResponse(BaseModel):
    """The shared logged-in response for both email-OTP login
    (``POST /auth/access-tokens-by-code``) and invite-gated
    registration (``POST /users``) — a JWT is issued and the frontend
    logs the user straight in."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["logged_in"] = "logged_in"
    access_token: str
    token_type: str
    user: UserResponse
