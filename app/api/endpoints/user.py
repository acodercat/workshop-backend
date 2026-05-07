"""User endpoints — own profile, password change, per-user SSE feed.

Admin user-listing was removed: the frontend never called it, and
keeping unused endpoints invites scope creep and security regressions
(forgotten auth, slow queries, etc.). Re-add when there is an actual
admin UI, ideally with a dedicated search index instead of ``ilike``.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import get_db, get_redis_streaming
from app.auth import (
    authenticate_and_get_user,
    authenticate_for_streaming,
    get_password_hash,
    revoke_user_tokens,
    verify_password,
)
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.user import PasswordChange, UserResponse
from app.services.user import stream_user_events

router = APIRouter()


@router.get("/me", response_model=UserResponse)
async def get_current_user(
    current_user: User = Depends(authenticate_and_get_user),
):
    return current_user


@router.get("/me/events")
async def stream_current_user_events(
    redis: aioredis.Redis = Depends(get_redis_streaming),
    current_user: User = Depends(authenticate_for_streaming),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    """Long-lived SSE stream of cross-session notifications for the
    authenticated user. Receives ``session.status_changed`` events for
    any session this user owns, regardless of which page the client is
    on. The generator runs until the HTTP client disconnects.

    ``Last-Event-ID`` is sent by the browser on reconnect; see
    :func:`stream_user_events` for the replay semantics.

    Auth uses :func:`authenticate_for_streaming` (not the standard
    request-scoped variant) so the JWT lookup's DB session closes before
    the SSE response begins — see that function's docstring for why.
    """
    return EventSourceResponse(
        stream_user_events(
            redis, current_user.id, last_event_id=last_event_id,
        )
    )


@router.put("/me/password", response_model=UserResponse)
@limiter.limit("5/minute")
async def change_password(
    request: Request,
    data: PasswordChange,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    if not await verify_password(data.current_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )
    # Refuse no-op changes: a same-password "update" would still bump
    # ``token_version`` and kick the user out of every active session
    # for nothing. Surface the no-op as a 400 instead.
    if await verify_password(data.new_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must differ from the current one",
        )
    current_user.password_hash = await get_password_hash(data.new_password)
    # Bump the token version so previously issued JWTs stop validating.
    revoke_user_tokens(current_user)
    await db.commit()
    await db.refresh(current_user)
    return current_user
