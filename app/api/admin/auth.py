"""Admin-console sign-in.

``POST /auth/access-tokens`` — exchange credentials for a
``scope="staff"`` token. That is the whole module: username and password,
no OTP, no invite, no self-registration. Staff accounts are provisioned
out of band (``scripts/manage_staff.py``), never created by whoever shows
up.

The signed-in operator's own resources are :mod:`app.api.admin.me`.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.dependencies import caller_ip
from app.api.dependencies import get_db
from app.auth import authenticate_staff, create_staff_access_token, is_staff_active
from app.errors import StaffCredentialsError
from app.rate_limit import limiter
from app.schemas.admin.auth import StaffLoginRequest, StaffTokenResponse
from app.schemas.admin.staff import StaffProfileResponse
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


@router.post("/auth/access-tokens", response_model=StaffTokenResponse)
@limiter.limit(settings.rate_limit_admin_login)
async def sign_in(
    request: Request,
    body: StaffLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> StaffTokenResponse:
    """Exchange staff credentials for an access token.

    A wrong password and a deactivated account produce the **same**
    error: a former operator probing the console should not be able to
    confirm their account still exists.

    A successful sign-in stamps ``last_login_at`` — the only trace this
    deployment keeps of who was in the console, since it keeps no
    operator trail.

    A refused attempt goes to the *log* only, and says more there than
    the response does: pinning the attempt on the account it targeted
    would record something its owner never did. The log line carries what
    the response withholds — which of the uniform 401's causes actually
    fired — plus the caller address; the correlation id is already bound
    to every line.
    """
    account = await authenticate_staff(db, body.username, body.password)
    if account is None or not is_staff_active(account):
        logger.warning(
            "Console sign-in refused for %r from %s: %s",
            body.username,
            caller_ip(request),
            "unknown account or wrong password"
            if account is None
            else "account disabled",
        )
        raise StaffCredentialsError("Incorrect username or password")

    token, expires_at = create_staff_access_token(account)

    account.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    return StaffTokenResponse(
        access_token=token,
        expires_at=expires_at,
        profile=StaffProfileResponse.model_validate(account),
    )
