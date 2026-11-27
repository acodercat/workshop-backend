from typing import TYPE_CHECKING

import redis.asyncio as aioredis
from fastapi import Request

from app.database import AsyncSessionLocal

# Annotations only. This module is imported by :mod:`app.auth`, so importing
# a service at runtime would pull the service layer in underneath it.
if TYPE_CHECKING:
    from app.services.identity.provider import MeetingIdentityProvider
    from app.services.meeting.clients import MeetingClients
    from app.services.user.hub import UserEventHub


async def get_db():
    """Request-scoped DB session.

    Explicit rollback on exception. ``async with AsyncSessionLocal()``
    would also rollback on ``__aexit__``, but spelling it out makes the
    contract obvious to readers and protects against handlers that
    swallow exceptions before yielding back here.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_redis(request: Request) -> aioredis.Redis:
    """Short-op Redis client — XADD, PUBLISH, DELETE, PING."""
    return request.app.state.redis


async def get_redis_streaming(request: Request) -> aioredis.Redis:
    """Long-poll Redis client — XREAD with block_ms=15s for SSE generators.

    Separate pool so a thundering herd of SSE clients can't starve the
    short-op path (enqueue, publish, cancel) by holding every connection.
    """
    return request.app.state.redis_streaming


async def get_user_event_hub(request: Request) -> "UserEventHub":
    """Process-wide :class:`~app.services.user.hub.UserEventHub`.

    Built once in the app lifespan; fans the per-user notification
    channel out in-process to every ``/me/events`` SSE connection.
    """
    return request.app.state.user_event_hub


async def get_identity_provider(request: Request) -> "MeetingIdentityProvider":
    """Process-wide :class:`~app.services.identity.provider.MeetingIdentityProvider`,
    built once in the app lifespan, which also owns its HTTP connections."""
    return request.app.state.identity_provider


async def get_meeting_clients(request: Request) -> "MeetingClients":
    """Process-wide :class:`~app.services.meeting.clients.MeetingClients`,
    built in the app lifespan."""
    return request.app.state.meeting_clients
