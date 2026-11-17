"""The enqueue's reset of the session's output stream is best-effort.

The SSE reader drops entries stamped with another turn's id, so a stream
the DELETE left behind is harmless — while a DELETE that raised would refuse
every message for as long as Redis is down.
"""

from unittest.mock import AsyncMock


async def test_a_redis_outage_does_not_refuse_the_message(run_enqueue) -> None:
    redis = AsyncMock()
    redis.delete.side_effect = ConnectionError("redis is down")

    await run_enqueue(redis)

    # Past the commit: the wake and the RUNNING announcement still go out.
    redis.publish.assert_awaited()
