from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str


# ---- registration ---------------------------------------------------------

# Login uses ``username`` as the OAuth2 ``form_data.username`` —
# registration introduces it explicitly. Constraints:
#
# - ``^[a-zA-Z0-9_-]{3,30}$`` — lowercase + uppercase + digits + ``_``
#   + ``-``. No dots, spaces, or unicode. Stored case-sensitively;
#   the validator just rejects garbage at the boundary.
# - ``email`` uses ``EmailStr`` (RFC 5322 via ``email-validator``).
# - ``password`` ≥ 6, ≤ 72 (bcrypt's input cap). Two-password
#   confirmation is purely a frontend concern; the API takes one.
#
# A separate ``locale`` field on the code-request payload lets the
# server pick which template to render — frontend passes its current
# i18n locale; ``zh-CN`` fallback if absent.

_USERNAME_PATTERN = r"^[a-zA-Z0-9_-]{3,30}$"


class RegisterCodeRequest(BaseModel):
    """Body for ``POST /auth/register/code`` — request a verification
    code be sent to ``email``."""

    email: EmailStr
    locale: Literal["zh-CN", "en-US"] = "zh-CN"


class RegisterRequest(BaseModel):
    """Body for ``POST /auth/register`` — complete registration."""

    email: EmailStr
    code: str = Field(..., pattern=r"^\d{6}$")
    username: str = Field(..., pattern=_USERNAME_PATTERN)
    name: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=6, max_length=72)
