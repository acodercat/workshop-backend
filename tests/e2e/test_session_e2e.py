"""End-to-end test: full backend → worker → agent → SSE round-trip.

Drives a real session through the production pipeline:

1. Create a session (HTTP)
2. POST a user message (HTTP)
3. Subscribe to the per-session SSE stream (HTTP)
4. Collect every event until the terminal ``agent.stopped``
5. Verify the persisted final state via ``GET /sessions/{id}``
6. Soft-delete the session (HTTP); confirm 404 on re-fetch and
   absence from the list endpoint

The ``test_user`` fixture provisions a throwaway user and tears down
every row that user owns (cascade through session → message /
session_turn, plus an explicit sandbox sweep) regardless of pass/fail —
see ``tests/conftest.py``.

Skipped unless ``RUN_E2E_TESTS=1`` is set; preconditions documented
in ``backend/CLAUDE.md``.
"""

import httpx

from tests.conftest import requires_e2e
from tests.e2e._helpers import (
    assert_event_envelope_clean,
    assert_tool_calls_paired,
    consume_sse_until_stopped,
)


# Deterministic, tool-free prompt. The default agent system prompt
# (axon) encourages tool use; this phrasing nudges the model toward a
# direct text reply so the assertion against assistant-content stays
# stable. Even so, the test does not assert on the *content* of the
# reply — only on the event sequence and terminal state — so a model
# that does decide to invoke a tool still passes.
_PROMPT = "请用中文回复 'OK' 两个字符，不要使用任何工具，不要任何额外内容。"

# Cap on how long we wait for the worker + agent to produce a terminal
# event. Sandbox cold-start (~5-10s) + LLM round-trip (~3-15s for a
# short prompt) + safety margin.
_SSE_TIMEOUT_SECONDS = 180.0


@requires_e2e
async def test_full_session_lifecycle(
    http_client: httpx.AsyncClient,
    test_user: dict,
) -> None:
    """Happy-path round-trip: create → message → SSE → verify → delete."""

    # 1. Create the session. No tier sent → defaults to STANDARD, which the
    #    server resolves to a model_id via ``settings.session_tier_models``.
    resp = await http_client.post(
        "/sessions",
        json={"title": "E2E test session"},
    )
    assert resp.status_code == 201, resp.text
    session = resp.json()
    session_id = session["id"]
    # Initial status on a brand-new session is IDLE (no turn queued
    # yet). The first POST to /messages flips it to RUNNING; one of
    # three terminals replaces RUNNING when the agent stops.
    assert session["status"] == "IDLE", session
    # No tier sent → STANDARD default, round-tripped on the response
    # (the resolved model_id stays server-side and is not exposed).
    assert session["tier"] == "STANDARD", session

    # 2. Enqueue a user message. Backend flips status to RUNNING and
    #    publishes the wake notification before this returns.
    resp = await http_client.post(
        f"/sessions/{session_id}/messages",
        json={"content": _PROMPT},
    )
    assert resp.status_code == 201, resp.text

    # 3. Stream events until the terminal ``agent.stopped`` arrives.
    events = await consume_sse_until_stopped(
        http_client, session_id, timeout=_SSE_TIMEOUT_SECONDS,
    )
    event_types = [t for t, _ in events]

    # 4. Wire-shape contracts that hold for every well-formed turn.
    assert_event_envelope_clean(events)
    assert_tool_calls_paired(events)

    # At least one content-bearing event between start and stop, so
    # we know the LLM actually produced output (vs. the worker bailing
    # before any model call). ``agent.message_chunk`` is the streamed
    # text path; ``agent.message`` is the post-stream rollup; either
    # is sufficient evidence.
    assert any(
        t in {"agent.message_chunk", "agent.message"}
        for t in event_types
    ), f"no agent message events in stream: {event_types}"

    # 5. Verify persisted DB state matches what the stream told us.
    resp = await http_client.get(f"/sessions/{session_id}")
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["status"] == "COMPLETE", (
        f"expected COMPLETE post-stop, got {detail['status']}"
    )
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["USER", "ASSISTANT"], roles

    # 6. Soft-delete: 204, then the session 404s on direct access AND
    #    is absent from the user's list (transparent soft-delete — the
    #    row is retained server-side but invisible to the user).
    resp = await http_client.delete(f"/sessions/{session_id}")
    assert resp.status_code == 204, resp.text
    resp = await http_client.get(f"/sessions/{session_id}")
    assert resp.status_code == 404, resp.text
    resp = await http_client.get("/sessions", params={"page": 1, "page_size": 100})
    assert resp.status_code == 200, resp.text
    listed_ids = [s["id"] for s in resp.json()["items"]]
    assert session_id not in listed_ids, (
        f"soft-deleted session still listed: {session_id} in {listed_ids}"
    )
