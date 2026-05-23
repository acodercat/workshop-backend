"""Unit tests for the event-driven settle subscriber.

Per-message tests only — ``run_forever``'s outer subscribe/reconnect
loop is straight Redis-client adapter code, not worth a unit test.
The contract under test is: ``_handle_message`` is robust against
the three bad-message flavors (bad payload / settle raises / DB
raises) and routes valid UUIDs through ``settle_open_for_session``.
"""

from unittest.mock import AsyncMock

import pytest

from app.services.redis import SETTLE_NOTIFY_CHANNEL
from app.workers import settle_subscriber


class _StubSession:
    """Stand-in for an ``AsyncSession`` — only ``commit`` + async-context
    methods are exercised in the subscriber path."""

    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True

    async def __aenter__(self) -> "_StubSession":
        return self

    async def __aexit__(self, *args) -> bool:
        return False


class _StubSessionFactory:
    """Replaces ``AsyncSessionLocal``. Tracks the most recent session
    so the test can assert ``commit()`` ran."""

    def __init__(self) -> None:
        self.last_session: _StubSession | None = None

    def __call__(self) -> _StubSession:
        self.last_session = _StubSession()
        return self.last_session


def test_settle_notify_channel_wire_constant() -> None:
    """The channel name is the cross-repo wire contract — guard against
    silent drift between backend and agent-worker."""
    assert SETTLE_NOTIFY_CHANNEL == "settle:notify"


async def test_handle_message_settles_on_valid_uuid(monkeypatch) -> None:
    factory = _StubSessionFactory()
    monkeypatch.setattr(settle_subscriber, "AsyncSessionLocal", factory)
    settle = AsyncMock()
    monkeypatch.setattr(settle_subscriber.credits, "settle_open_for_session", settle)

    sid = "11111111-2222-3333-4444-555555555555"
    await settle_subscriber._handle_message(sid)

    settle.assert_awaited_once()
    db_arg, sid_arg = settle.await_args.args
    assert db_arg is factory.last_session
    assert str(sid_arg) == sid
    assert factory.last_session.committed is True


async def test_handle_message_decodes_bytes_payload(monkeypatch) -> None:
    """Tolerate ``bytes`` payload in case ``decode_responses=False`` is
    ever flipped on the shared client."""
    factory = _StubSessionFactory()
    monkeypatch.setattr(settle_subscriber, "AsyncSessionLocal", factory)
    settle = AsyncMock()
    monkeypatch.setattr(settle_subscriber.credits, "settle_open_for_session", settle)

    sid = "11111111-2222-3333-4444-555555555555"
    await settle_subscriber._handle_message(sid.encode("utf-8"))

    settle.assert_awaited_once()
    assert str(settle.await_args.args[1]) == sid


@pytest.mark.parametrize(
    "bad_payload",
    [
        "not-a-uuid",
        "",
        "00000000-0000",  # truncated
        b"\xff\xfe",      # non-UTF8-ish bytes that still decode but aren't a UUID
    ],
)
async def test_handle_message_skips_invalid_payload(monkeypatch, bad_payload) -> None:
    factory = _StubSessionFactory()
    monkeypatch.setattr(settle_subscriber, "AsyncSessionLocal", factory)
    settle = AsyncMock()
    monkeypatch.setattr(settle_subscriber.credits, "settle_open_for_session", settle)

    # Must not raise — subscriber loop has to survive bad messages.
    await settle_subscriber._handle_message(bad_payload)

    settle.assert_not_awaited()
    assert factory.last_session is None  # never opened a DB session


async def test_handle_message_survives_settle_exception(monkeypatch) -> None:
    """A DB / lock / settle error mid-handler must not propagate —
    the subscriber loop has to keep running so the next message
    processes. The sweeper backstops the lost settle."""
    factory = _StubSessionFactory()
    monkeypatch.setattr(settle_subscriber, "AsyncSessionLocal", factory)
    settle = AsyncMock(side_effect=RuntimeError("simulated DB blip"))
    monkeypatch.setattr(settle_subscriber.credits, "settle_open_for_session", settle)

    sid = "11111111-2222-3333-4444-555555555555"
    # Must not raise.
    await settle_subscriber._handle_message(sid)

    settle.assert_awaited_once()
    # Session was opened, commit never fired (settle raised first).
    assert factory.last_session is not None
    assert factory.last_session.committed is False
