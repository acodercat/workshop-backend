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
    """Body for ``POST /users`` — complete registration via the
    email-OTP flow."""

    email: NormalizedEmail
    code: str = Field(..., pattern=r"^\d{6}$")
    name: str = Field(..., min_length=1, max_length=255)


#
# The unified entry point: ``POST /auth/access-tokens-by-code`` peek-
# verifies the code (no consume) and reports whether the email
# already maps to a user. Existing user → returns a JWT immediately;
# new user → returns a marker telling the frontend to collect a
# display name and call ``POST /users`` (which consumes the code and
# creates the row).


class EmailLoginRequest(BaseModel):
    """Body for ``POST /auth/access-tokens-by-code``."""

    email: NormalizedEmail
    code: str = Field(..., pattern=r"^\d{6}$")


class EmailLoginExisting(BaseModel):
    """Response when ``email`` is already a registered user — JWT
    issued, frontend logs them straight in."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["logged_in"] = "logged_in"
    access_token: str
    token_type: str
    user: UserResponse


class EmailLoginNewUser(BaseModel):
    """Response when ``email`` isn't registered yet — the code peek
    succeeded, frontend collects a name and calls ``POST /users``."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["registration_required"] = "registration_required"


# Discriminated union the endpoint returns. FastAPI / Pydantic v2
# honours the ``kind`` discriminator when serializing.
EmailLoginResponse = EmailLoginExisting | EmailLoginNewUser


class RegisterResponse(BaseModel):
    """Response from ``POST /users`` after email-OTP registration.

    Email-OTP creates the user with ``status=PENDING`` — access is
    gated on an admin approval step, so we don't issue a JWT here.
    The frontend pivots from the registration form to a "thanks,
    awaiting approval" screen on this response. ``kind`` is a
    forward-compat discriminator: if a future flow lets approved
    invitees skip pending, that path returns ``kind="logged_in"``
    with a token field instead."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["pending_approval"] = "pending_approval"
