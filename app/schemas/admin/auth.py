"""Request / response models for admin-console sign-in.

The operator's own profile lives in :mod:`app.schemas.admin.staff` with
the rest of the account shapes; sign-in only borrows it for the token
response.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.admin.staff import StaffProfileResponse


class StaffLoginRequest(BaseModel):
    """Staff sign-in credentials.

    A JSON body rather than the OAuth2 form the customer login accepts:
    the console is a purpose-built SPA with no form-encoded compatibility
    to preserve, and JSON keeps it consistent with every other admin
    endpoint.
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class StaffTokenResponse(BaseModel):
    """A freshly issued staff access token.

    ``expires_at`` is absolute rather than a lifetime in seconds, so the
    client needn't reason about clock drift between issue and receipt;
    ``profile`` rides along so the console can render its header without
    a second round trip.
    """

    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    profile: StaffProfileResponse
