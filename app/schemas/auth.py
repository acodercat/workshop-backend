from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.common.email import NormalizedEmail
from app.schemas.user import UserResponse


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str


#
# Code flows (registration + login) identify the account by email, and the
# code is delivered over SMTP. The ``axon_${id}`` username and a random
# password placeholder are generated server-side. ``locale`` on the
# code-request payload picks the email template.


class _IdentifierBody(BaseModel):
    """The email that identifies the account."""

    email: NormalizedEmail


class RegisterCodeRequest(_IdentifierBody):
    """Body for ``POST /auth/verification-codes`` — send a verification
    code to ``email``."""

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
