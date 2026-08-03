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

from app.api.admin.dependencies import build_context, caller_ip
from app.api.dependencies import get_db
from app.auth import authenticate_staff, create_staff_access_token, is_staff_active
from app.errors import StaffCredentialsError
from app.rate_limit import limiter
from app.schemas.admin.auth import StaffLoginRequest, StaffTokenResponse
from app.schemas.admin.staff import StaffProfileResponse
from app.services.admin import AuditAction, audit
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


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

    A successful sign-in stamps ``last_login_at`` and records a
    ``staff.login`` row — "who was in the console that afternoon" is a
    question incident review actually asks. It commits through the same
    unit of work as every other console write, so the timestamp and the
    audit row cannot disagree.

    A refused attempt goes to the *log*, not the audit trail: an audit
    row is an action by an operator (``staff_id`` is ``NOT NULL``), and
    pinning the attempt on the account it targeted would record something
    its owner never did. The log line carries what the response withholds
    — which of the uniform 401's causes actually fired — plus the caller
    address; the correlation id is already bound to every line.
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

    ctx = build_context(request, db=db, staff=account)
    async with audit(
        ctx,
        action=AuditAction.STAFF_LOGIN,
        target_type="staff",
        target_id=account.id,
    ):
        account.last_login_at = datetime.now(timezone.utc)

    return StaffTokenResponse(
        access_token=token,
        expires_at=expires_at,
        profile=StaffProfileResponse.model_validate(account),
    )
