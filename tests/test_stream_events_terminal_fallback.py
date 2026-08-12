"""Regression tests for the SSE terminal-liveness fallback (review M3).

The follow loop in :func:`stream_events` only exits on a Redis
``agent.stopped`` event. But the worker's stream writes are best-effort
(Postgres is the durable truth), so a Redis blip can swallow the terminal
XADD, and a crash can drop it between the fenced DB commit and the emit.
Either way the loop would block forever and hang the SSE client. The
fallback consults ``session.status`` after an idle XREAD block and
synthesizes the terminal when the DB is already terminal — without
false-closing a legitimately slow but still-RUNNING turn.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import json
from unittest.mock import AsyncMock

import pytest

from app.common.event_types import EventType
from app.services.session import streaming
from app.services.session.streaming import (
    _BLIND_FUSE_SECONDS,
    _Liveness,
    _safe_cursor,
)


@pytest.fixture(autouse=True)
def _disable_turn_filter(monkeypatch):
    """Default the turn-id filter off (no DB) so the existing stream tests
    stay offline — they carry no ``turn_id`` on their entries anyway, so
    ``None`` (filter disabled) is the correct baseline. The turn-filter
    tests override this with a concrete current turn id."""
    monkeypatch.setattr(
        streaming,
        "_current_turn_id",
        AsyncMock(return_value=None),
    )


async def _collect(gen, limit: int = 20) -> list[dict]:
    """Drain an SSE generator with a hard cap so a regression that fails
    to terminate surfaces as a bounded list, not a hung test."""
    out: list[dict] = []
    async for item in gen:
        out.append(item)
        if len(out) >= limit:
            break
    return out


async def test_idle_stream_with_terminal_db_synthesizes_stopped(monkeypatch):
    """Terminal in the DB, no terminal on the stream → synthesize + close.

    Simulates the swallowed-XADD / crash-after-commit case: replay is
    empty, every follow read is idle, and the DB reports terminal. The
    generator must emit one synthetic ``agent.stopped`` (empty content,
    matching ``emit_terminal_done`` / recovery-cron shape) and return,
    rather than blocking forever."""
    redis = AsyncMock()
    redis.xrange = AsyncMock(return_value=[])  # no replay history
    monkeypatch.setattr(streaming, "read_stream", AsyncMock(return_value=[]))  # idle block
    monkeypatch.setattr(
        streaming,
        "_session_liveness",
        AsyncMock(return_value=_Liveness.TERMINAL),
    )

    events = await _collect(streaming.stream_events(redis, "sess-1"))

    # Generator terminated (well under the cap) with exactly one terminal,
    # and it's the synthesized empty-content one.
    assert len(events) < 20
    assert events[-1]["event"] == EventType.AGENT_STOPPED.value
    assert json.loads(events[-1]["data"]) == {"content": ""}
    terminals = [e for e in events if e["event"] == EventType.AGENT_STOPPED.value]
    assert len(terminals) == 1


async def test_idle_stream_still_running_does_not_false_close(monkeypatch):
    """A slow but live turn idles past a block; the DB still says RUNNING,
    so the loop must NOT synthesize a terminal — it keeps following and
    forwards the *real* ``agent.stopped`` when it finally arrives."""
    redis = AsyncMock()
    redis.xrange = AsyncMock(return_value=[])

    real_terminal = [
        (
            "stream-key",
            [("5-0", {"type": "agent.stopped", "content": "done"})],
        )
    ]
    # First follow read: idle (empty). Second: the real terminal arrives.
    monkeypatch.setattr(
        streaming,
        "read_stream",
        AsyncMock(side_effect=[[], real_terminal]),
    )
    liveness = AsyncMock(return_value=_Liveness.RUNNING)  # still RUNNING at the idle check
    monkeypatch.setattr(streaming, "_session_liveness", liveness)

    events = await _collect(streaming.stream_events(redis, "sess-1"))

    # Consulted the DB once after the idle block, saw RUNNING, did not
    # false-close — the terminal that closed the stream is the real event.
    liveness.assert_awaited_once()
    assert events[-1]["event"] == EventType.AGENT_STOPPED.value
    assert json.loads(events[-1]["data"]) == {"content": "done"}


async def test_blind_dual_outage_trips_the_fuse(monkeypatch):
    """Redis idle *and* DB unreachable past the fuse window → close, no
    synthetic terminal.

    Simulates a sustained dual outage: every follow read is idle and every
    liveness check returns UNKNOWN. The generator must NOT block for the
    full stream TTL — once it has been fully blind past
    ``_BLIND_FUSE_SECONDS`` it returns, *without* fabricating an
    ``agent.stopped`` (it doesn't actually know the turn stopped), so the
    client's EventSource reconnects and re-evaluates."""
    redis = AsyncMock()
    redis.xrange = AsyncMock(return_value=[])
    monkeypatch.setattr(streaming, "read_stream", AsyncMock(return_value=[]))  # always idle
    monkeypatch.setattr(
        streaming,
        "_session_liveness",
        AsyncMock(return_value=_Liveness.UNKNOWN),
    )
    # Deterministic monotonic clock: the first blind cycle records the start,
    # the second reads past the fuse window and trips it.
    ticks = iter([1.0, _BLIND_FUSE_SECONDS + 2.0] + [9_999.0] * 50)
    monkeypatch.setattr(streaming.time, "monotonic", lambda: next(ticks))

    events = await _collect(streaming.stream_events(redis, "sess-1"))

    # Closed (under the cap) and never fabricated a terminal under the outage.
    assert len(events) < 20
    assert all(e["event"] != EventType.AGENT_STOPPED.value for e in events)


def test_safe_cursor_passes_valid_entry_id():
    """A well-formed ``<ms>-<seq>`` id round-trips unchanged."""
    assert _safe_cursor("1700000000000-0") == "1700000000000-0"
    assert _safe_cursor("5-12") == "5-12"


def test_safe_cursor_rejects_malformed_id():
    """Anything that isn't a clean entry id → None (full replay), so a
    client-controlled header can't inject range syntax into xrange(min=)."""
    for bad in [None, "", "(5-0", "+", "-", "5", "abc", "5-0; DROP", "5-"]:
        assert _safe_cursor(bad) is None


def _chunk(i: int) -> tuple[str, dict]:
    """A replay entry shaped ``(entry_id, fields)`` as Redis xrange returns it."""
    return f"{i}-0", {"type": "agent.message_chunk", "content": f"c{i}", "ts": "1"}


async def test_replay_spans_multiple_pages_in_order(monkeypatch):
    """A backlog larger than one page is drained across successive ``xrange``
    calls, each resuming from an exclusive cursor past the last entry — every
    event forwarded once, in order, with no overlap or skip."""
    monkeypatch.setattr(streaming, "_REPLAY_PAGE_SIZE", 2)
    redis = AsyncMock()
    # Two full pages then a short one — the short page ends replay without a
    # trailing empty xrange.
    redis.xrange = AsyncMock(
        side_effect=[[_chunk(1), _chunk(2)], [_chunk(3), _chunk(4)], [_chunk(5)]]
    )
    # Phase 2 closes the stream straight away so the test targets replay only.
    terminal = [("k", [("9-0", {"type": "agent.stopped", "content": "done"})])]
    monkeypatch.setattr(
        streaming,
        "read_stream",
        AsyncMock(return_value=terminal),
    )

    events = await _collect(streaming.stream_events(redis, "sess-1"))

    # All five replayed chunks, in order.
    contents = [
        json.loads(e["data"])["content"] for e in events if e["event"] == "agent.message_chunk"
    ]
    assert contents == ["c1", "c2", "c3", "c4", "c5"]
    # Three pages read; the short final page skipped a fourth (empty) call.
    assert redis.xrange.await_count == 3
    # Each call resumed strictly after the previous page's last entry.
    mins = [c.kwargs["min"] for c in redis.xrange.await_args_list]
    assert mins == ["-", "(2-0", "(4-0"]


def test_is_stale_entry_logic():
    # Mismatch → stale; match → not; missing stamp or unresolved current
    # turn → never stale (fail-open, so a real event is never dropped).
    assert streaming._is_stale_entry({"turn_id": "5"}, "6") is True
    assert streaming._is_stale_entry({"turn_id": "6"}, "6") is False
    assert streaming._is_stale_entry({}, "6") is False
    assert streaming._is_stale_entry({"turn_id": "5"}, None) is False


async def test_replay_drops_stale_turn_entries(monkeypatch):
    """A zombie worker (turn 19) leaked events — including a stale terminal —
    into the stream key the current turn (20) recreated. The stale entries
    are dropped, the stale ``agent.stopped`` does NOT close the stream, and
    the internal ``turn_id`` stamp never reaches the client."""
    monkeypatch.setattr(
        streaming,
        "_current_turn_id",
        AsyncMock(return_value="20"),
    )
    redis = AsyncMock()
    redis.xrange = AsyncMock(
        side_effect=[
            [
                (
                    "1-0",
                    {"type": "agent.message_chunk", "content": "ghost", "ts": "1", "turn_id": "19"},
                ),
                (
                    "2-0",
                    {"type": "agent.message_chunk", "content": "real", "ts": "1", "turn_id": "20"},
                ),
                ("3-0", {"type": "agent.stopped", "content": "stale", "ts": "1", "turn_id": "19"}),
                ("4-0", {"type": "agent.stopped", "content": "done", "ts": "1", "turn_id": "20"}),
            ]
        ]
    )
    monkeypatch.setattr(streaming, "read_stream", AsyncMock(return_value=[]))

    events = await _collect(streaming.stream_events(redis, "sess-1"))

    contents = [
        json.loads(e["data"])["content"] for e in events if e["event"] == "agent.message_chunk"
    ]
    assert contents == ["real"]  # turn-19 ghost dropped
    terminals = [e for e in events if e["event"] == EventType.AGENT_STOPPED.value]
    assert len(terminals) == 1  # stale terminal didn't close us
    assert json.loads(terminals[0]["data"])["content"] == "done"
    # The routing stamp is stripped from every forwarded payload.
    assert all("turn_id" not in json.loads(e["data"]) for e in events if e.get("data"))


async def test_replay_terminal_returns_before_paging_on(monkeypatch):
    """An ``agent.stopped`` inside a replay page closes the stream immediately
    — no further pages, no follow loop."""
    monkeypatch.setattr(streaming, "_REPLAY_PAGE_SIZE", 2)
    redis = AsyncMock()
    redis.xrange = AsyncMock(
        side_effect=[[_chunk(1), ("2-0", {"type": "agent.stopped", "content": "done"})]]
    )
    follow = AsyncMock(return_value=[])
    monkeypatch.setattr(streaming, "read_stream", follow)

    events = await _collect(streaming.stream_events(redis, "sess-1"))

    assert events[-1]["event"] == EventType.AGENT_STOPPED.value
    assert json.loads(events[-1]["data"]) == {"content": "done"}
    redis.xrange.assert_awaited_once()  # returned mid-page, never paged on
    follow.assert_not_awaited()  # never reached the follow loop
