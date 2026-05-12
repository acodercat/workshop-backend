"""Shared helpers for E2E tests — SSE consumer + event-shape predicates.

Kept private (leading underscore) because it's an implementation
detail of the E2E suite, not a fixture. Importable from any test in
``tests/e2e/``.
"""

import json

import httpx
import pytest


async def consume_sse_until_stopped(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout: float,
) -> list[tuple[str, dict]]:
    """Consume the per-session SSE stream up to (and including) the
    terminal ``agent.stopped`` event.

    Each returned tuple is ``(event_type, data_dict)`` — ``data`` is
    JSON-decoded once at parse time so callers don't re-parse for
    every assertion. Note that nested fields the worker JSON-encoded
    a second time (``files``, ``args``, ``usage``) stay as JSON
    strings; callers decode them on demand.

    The backend's per-connection ``session.ready`` handshake is
    dropped here — it's a transport-layer marker that fires on every
    reconnect, not part of the agent's event stream. Surfacing it
    would force every assertion to special-case it.

    Parses the three SSE line shapes the backend emits — ``event:``,
    ``data:``, ``id:`` — plus the spec-mandated blank dispatch line.
    Comment lines (``:keepalive``) are ignored.

    Fails the test (rather than returning) if the stream closes
    without an ``agent.stopped``: a silent return would let pairing
    assertions misfire on a partial event log.
    """
    events: list[tuple[str, dict]] = []
    current_event: str | None = None
    current_data: list[str] = []

    async with client.stream(
        "GET", f"/sessions/{session_id}/stream", timeout=timeout,
    ) as response:
        assert response.status_code == 200, (
            f"SSE open failed: {response.status_code} "
            f"{await response.aread()!r}"
        )
        async for line in response.aiter_lines():
            if not line:
                if current_event is not None and current_event != "session.ready":
                    payload = (
                        json.loads("\n".join(current_data))
                        if current_data else {}
                    )
                    events.append((current_event, payload))
                    if current_event == "agent.stopped":
                        return events
                current_event = None
                current_data = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                current_event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                current_data.append(line[len("data:"):].lstrip())

    pytest.fail(
        f"SSE stream closed without an agent.stopped event "
        f"(collected {len(events)} events: "
        f"{[t for t, _ in events]})"
    )


def assert_event_envelope_clean(events: list[tuple[str, dict]]) -> None:
    """Common shape contract every well-formed turn must satisfy.

    - The first **worker-side** event is the synthetic
      ``agent.status: started`` lifecycle hook from the runner (always
      emitted before any agent work). ``session.ready`` is a
      per-connection backend handshake injected by the SSE endpoint
      before the worker's stream even starts — skipped here, since
      it's an artifact of the channel, not the agent lifecycle.
    - Last event is ``agent.stopped``.
    - The terminal event's ``stop_reason`` is not the error fallback —
      either an explicit happy reason (``"end_turn"``, ``"stop"``) or
      absent (synthetic emit_terminal_done from the error path uses
      no stop_reason; we only reject the literal ``"error"`` value).

    Use this in every E2E test before drilling into format-specific
    assertions, so failure messages point at the envelope rather than
    a misleading downstream check.
    """
    assert events, "no events collected"

    worker_events = [
        (t, p) for t, p in events if t != "session.ready"
    ]
    assert worker_events, "no worker-side events collected (only handshakes)"

    first_type, first_payload = worker_events[0]
    assert first_type == "agent.status", (
        f"first worker event must be agent.status, got {first_type}"
    )
    assert first_payload.get("status") == "started", (
        f"first agent.status must be 'started', got {first_payload}"
    )

    last_type, last_payload = worker_events[-1]
    assert last_type == "agent.stopped", (
        f"last event must be agent.stopped, got {last_type}"
    )
    assert last_payload.get("stop_reason") != "error", (
        f"agent stopped with error: {last_payload}"
    )


def assert_tool_calls_paired(events: list[tuple[str, dict]]) -> None:
    """Every ``agent.tool_use`` must have a matching ``agent.tool_result``
    with the same ``tool_use_id``.

    The worker's mid-stream cancellation safety guarantees this even on
    crashed turns (synthetic placeholder results). A missing pair here
    means either the worker emitted a malformed stream or the SSE
    consumer dropped events — both regressions worth catching.
    """
    tool_use_ids = {
        payload["tool_use_id"]
        for type_, payload in events
        if type_ == "agent.tool_use"
    }
    tool_result_ids = {
        payload["tool_use_id"]
        for type_, payload in events
        if type_ == "agent.tool_result"
    }
    orphan_uses = tool_use_ids - tool_result_ids
    orphan_results = tool_result_ids - tool_use_ids
    assert not orphan_uses, (
        f"agent.tool_use without matching tool_result: {orphan_uses}"
    )
    assert not orphan_results, (
        f"agent.tool_result without matching tool_use: {orphan_results}"
    )
