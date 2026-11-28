"""What every meeting operation runs with, and how its failures read.

A :class:`MeetingContext` is built per request: the Meeting client, the
signed-in user acting there, and the session that lets them. Operations
take it instead of four arguments, and :func:`meeting_errors` turns what
went wrong at Meeting into the application's typed errors in one place.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import redis.asyncio as aioredis

from app.errors import (
    MeetingGenerationFailedError,
    MeetingRateLimitedError,
    MeetingRecordUnreadableError,
    MeetingRefusedError,
    MeetingServiceUnavailableError,
    MeetingSessionExpiredError,
    TranscriptEditUnsafeError,
)
from app.integrations.meeting.meetings import MeetingMeetings
from app.integrations.meeting.output import (
    OutputEmpty,
    OutputFailed,
    OutputIncomplete,
)
from app.integrations.meeting.records import RecordInvalid, TranscriptEditUnsafe
from app.integrations.meeting.transport import (
    MeetingActor,
    MeetingDenied,
    MeetingRateLimited,
    MeetingRefused,
    MeetingUnavailable,
)
from app.services.identity.sessions import end_refused_session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MeetingContext:
    meetings: MeetingMeetings
    actor: MeetingActor
    # The user's account id at Meeting.
    account_id: str
    session_id: uuid.UUID
    user_id: int
    # For the short locks that keep two edits of one record apart.
    redis: aioredis.Redis


@asynccontextmanager
async def meeting_errors(ctx: MeetingContext) -> AsyncIterator[None]:
    """Translate Meeting's failures for the client.

    A denial is the one with a side effect: Meeting has ended the session
    behind this sign-in, so it is ended here too — otherwise the user would
    stay signed in to a workspace that can no longer show their meetings,
    until the next scheduled re-confirmation noticed. Unless a concurrent
    request rotated the session cookie after this one read it: then the
    refusal was of the superseded value, the session stands, and the
    request fails as retryable.
    """
    try:
        yield
    except MeetingDenied as exc:
        if await end_refused_session(ctx.session_id, refused=ctx.actor.session):
            raise MeetingSessionExpiredError("Meeting ended this session.") from exc
        raise MeetingServiceUnavailableError("Meeting refused a superseded session.") from exc
    except MeetingRateLimited as exc:
        raise MeetingRateLimitedError("Meeting is throttling requests.") from exc
    except MeetingRefused as exc:
        logger.info("meeting refused a request with code %s", exc.code)
        raise MeetingRefusedError(
            "Meeting declined the request.", details={"code": exc.code}
        ) from exc
    except MeetingUnavailable as exc:
        logger.warning("meeting unavailable: %s", exc)
        raise MeetingServiceUnavailableError("Meeting is unavailable.") from exc
    except RecordInvalid as exc:
        logger.warning("unreadable meeting record: %s", exc.reason)
        raise MeetingRecordUnreadableError(
            "Meeting returned a record that cannot be read.", details={"reason": exc.reason}
        ) from exc
    except TranscriptEditUnsafe as exc:
        raise TranscriptEditUnsafeError("The transcript cannot be edited safely.") from exc
    except OutputFailed as exc:
        raise MeetingGenerationFailedError(
            "Meeting did not produce an answer.", details={"reason": exc.reason}
        ) from exc
    except OutputIncomplete as exc:
        raise MeetingGenerationFailedError(
            "Meeting's answer was incomplete.", details={"reason": "incomplete"}
        ) from exc
    except OutputEmpty as exc:
        raise MeetingGenerationFailedError(
            "Meeting returned an empty answer.", details={"reason": "empty"}
        ) from exc
