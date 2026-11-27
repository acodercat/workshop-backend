"""``subscribe_forever``: each published payload reaches the handler as
text, and neither a failing handler nor a dropped connection ends it."""

import asyncio
from collections.abc import Iterator

import pytest

from app.workers import pubsub


class _PubSub:
    """One subscription, yielding ``messages`` and then blocking as an idle
    channel does. An exception among them is raised by the read."""

    def __init__(self, messages: Iterator) -> None:
        self._messages = messages
        self.channels: list[str] = []

    async def __aenter__(self) -> "_PubSub":
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def subscribe(self, channel: str) -> None:
        self.channels.append(channel)

    async def get_message(self, *, ignore_subscribe_messages: bool, timeout: float):
        try:
            data = next(self._messages)
        except StopIteration:
            await asyncio.Event().wait()
        if isinstance(data, Exception):
            raise data
        return {"data": data}


class _Redis:
    def __init__(self, *messages) -> None:
        self._messages = iter(messages)
        self.subscriptions: list[_PubSub] = []

    def pubsub(self) -> _PubSub:
        self.subscriptions.append(_PubSub(self._messages))
        return self.subscriptions[-1]


async def _received(redis: _Redis, count: int, *, failing: frozenset[str] = frozenset()) -> list:
    """Follow the channel until ``count`` payloads have reached the handler."""
    received: list[str] = []
    done = asyncio.Event()

    async def handle(payload: str) -> None:
        received.append(payload)
        if len(received) == count:
            done.set()
        if payload in failing:
            raise RuntimeError("handler failed")

    task = asyncio.create_task(pubsub.subscribe_forever(redis, "some:channel", handle))
    try:
        async with asyncio.timeout(1):
            await done.wait()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    return received


async def test_payloads_reach_the_handler_as_text() -> None:
    # ``bytes`` should ``decode_responses`` ever be turned off on the client.
    redis = _Redis("a", b"b")

    assert await _received(redis, 2) == ["a", "b"]
    assert redis.subscriptions[0].channels == ["some:channel"]


async def test_a_failing_handler_skips_only_its_message() -> None:
    redis = _Redis("a", "b")

    assert await _received(redis, 2, failing=frozenset({"a"})) == ["a", "b"]
    assert len(redis.subscriptions) == 1


async def test_a_dropped_connection_is_subscribed_again(monkeypatch) -> None:
    monkeypatch.setattr(pubsub, "_RECONNECT_DELAY_SECONDS", 0)
    redis = _Redis("a", ConnectionError("dropped"), "b")

    assert await _received(redis, 2) == ["a", "b"]
    assert len(redis.subscriptions) == 2
