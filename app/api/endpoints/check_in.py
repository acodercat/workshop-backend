"""Daily check-in endpoints (每日签到).

User-facing surface over the check-in service:

- ``GET  /check-ins/today`` — today's check-in resource: its state
  (claimed or not) + streak / total / reward, so the UI can render.
- ``POST /check-ins``       — claim today's reward; 409 if already claimed.

Credits on the wire are whole-credit integers (the service works in
µ-credits and converts at this boundary). Past check-in grants show up in
the credit transactions list (``GET /credits/transactions``) as
``CHECKIN`` rows, so there is no separate history endpoint.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.check_in import CheckInResponse, CheckInStatusResponse
from app.services import check_in, credits
from app.services.credits.rates import credits_from_micro

router = APIRouter()


@router.get("/check-ins/today", response_model=CheckInStatusResponse)
async def get_today_check_in(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> CheckInStatusResponse:
    """Return ``current_user``'s check-in resource for today."""
    status = await check_in.get_check_in_status(db, user_id=current_user.id)
    return CheckInStatusResponse(
        enabled=status.enabled,
        checked_in_today=status.checked_in_today,
        streak_day=status.streak_day,
        total_days=status.total_days,
        reward_credits=credits_from_micro(status.reward_micro),
    )


@router.post("/check-ins", response_model=CheckInResponse)
@limiter.limit("10/minute")
async def create_check_in(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> CheckInResponse:
    """Claim today's check-in reward. Raises 409 (``already_checked_in``)
    if today is already claimed, 403 (``check_in_disabled``) if the
    feature is off."""
    result = await check_in.claim_daily_check_in(db, user_id=current_user.id)
    await db.commit()
    balance_micro = await credits.get_balance(db, current_user.id)
    return CheckInResponse(
        reward_credits=credits_from_micro(result.reward_micro),
        streak_day=result.streak_day,
        balance_credits=credits_from_micro(balance_micro),
    )
