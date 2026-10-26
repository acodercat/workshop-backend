"""``session.status_changed`` for the transitions the backend writes itself.

The worker announces the terminals it writes; without the backend announcing
its own — ``RUNNING`` here, ``CANCELLED`` in
``test_session_cancel_is_idempotent``, ``FAILED`` in ``test_settle_sweeper`` —
the user's other tabs keep showing the old state until a refetch.
"""

import json
import uuid
from unittest.mock import AsyncMock

from app.models.session import SessionStatus
from app.services.redis import USER_EVENTS_CHANNEL
from app.services.user import publish_session_status


def _announced(redis: AsyncMock) -> dict:
    channel, raw = redis.publish.await_args.args
    assert channel == USER_EVENTS_CHANNEL
    return json.loads(raw)


async def test_the_payload_has_the_workers_shape() -> None:
    redis = AsyncMock()
    session_id = uuid.uuid4()

    await publish_session_status(
        redis, user_id=7, session_id=session_id, status=SessionStatus.RUNNING
    )

    payload = _announced(redis)
    assert payload["user_id"] == 7
    assert payload["event"]["type"] == "session.status_changed"
    assert payload["event"]["session_id"] == str(session_id)
    assert payload["event"]["status"] == "RUNNING"
    assert isinstance(payload["event"]["ts"], float)


async def test_a_failed_publish_does_not_raise() -> None:
    # Called after a commit: the write already happened and must not 500.
    redis = AsyncMock()
    redis.publish.side_effect = ConnectionError("redis down")

    await publish_session_status(
        redis, user_id=7, session_id=uuid.uuid4(), status=SessionStatus.FAILED
    )


async def test_enqueue_announces_running(run_enqueue) -> None:
    redis = AsyncMock()

    await run_enqueue(redis)

    payload = _announced(redis)
    assert payload["user_id"] == 7
    assert payload["event"]["status"] == "RUNNING"
