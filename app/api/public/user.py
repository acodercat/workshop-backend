"""User endpoints — own profile and the per-user SSE feed.

Nothing here changes a credential: the password and the email belong to
the identity provider and are changed there (a password reset is relayed
by ``POST /auth/password-resets``).

Admin user-listing was removed: the frontend never called it, and
keeping unused endpoints invites scope creep and security regressions
(forgotten auth, slow queries, etc.). Re-add when there is an actual
admin UI, ideally with a dedicated search index instead of ``ilike``.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import get_db, get_user_event_hub
from app.auth import authenticate_and_get_user, authenticate_for_streaming
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.user import ProfileUpdate, UserResponse
from app.services.user import UserEventHub, stream_user_events

router = APIRouter()


@router.get("/me")
async def get_current_user(
    current_user: User = Depends(authenticate_and_get_user),
) -> UserResponse:
    return UserResponse.model_validate(current_user)


@router.patch("/me")
@limiter.limit("10/minute")
async def update_profile(
    request: Request,
    data: ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> UserResponse:
    """Partial update of the caller's own profile.

    All fields in :class:`ProfileUpdate` are optional. ``model_dump
    (exclude_unset=True)`` distinguishes "field omitted" (leave column
    alone) from "field set to None" (clear the column).
    """
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(current_user, field, value)
    await db.commit()
    await db.refresh(current_user)
    return UserResponse.model_validate(current_user)


@router.get("/me/events")
async def stream_current_user_events(
    hub: UserEventHub = Depends(get_user_event_hub),
    current_user: User = Depends(authenticate_for_streaming),
):
    """Long-lived SSE stream of cross-session notifications for the
    authenticated user. Receives ``session.status_changed`` events for
    any session this user owns, regardless of which page the client is
    on. The generator runs until the HTTP client disconnects.

    Live-only: events are fanned out in-process from the per-user
    notification channel (see :class:`~app.services.user.hub.UserEventHub`);
    the page's initial sidebar state comes from ``GET /sessions``, so
    there is no ``Last-Event-ID`` replay.

    Auth uses :func:`authenticate_for_streaming` (not the standard
    request-scoped variant) so the JWT lookup's DB session closes before
    the SSE response begins — see that function's docstring for why.
    """
    return EventSourceResponse(stream_user_events(hub, current_user.id))
