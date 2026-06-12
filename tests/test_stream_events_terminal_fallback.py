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

from app.common.event_types import EventType
from app.services.session import streaming


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
        streaming, "_session_is_terminal", AsyncMock(return_value=True),
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

    real_terminal = [(
        "stream-key",
        [("5-0", {"type": "agent.stopped", "content": "done"})],
    )]
    # First follow read: idle (empty). Second: the real terminal arrives.
    monkeypatch.setattr(
        streaming, "read_stream", AsyncMock(side_effect=[[], real_terminal]),
    )
    is_terminal = AsyncMock(return_value=False)  # still RUNNING at the idle check
    monkeypatch.setattr(streaming, "_session_is_terminal", is_terminal)

    events = await _collect(streaming.stream_events(redis, "sess-1"))

    # Consulted the DB once after the idle block, saw RUNNING, did not
    # false-close — the terminal that closed the stream is the real event.
    is_terminal.assert_awaited_once()
    assert events[-1]["event"] == EventType.AGENT_STOPPED.value
    assert json.loads(events[-1]["data"]) == {"content": "done"}
