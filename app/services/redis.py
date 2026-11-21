"""Redis service layer — Streams for SSE output, Pub/Sub for worker wake + cancel.

Postgres ``session_turn`` is the queue; this module no longer carries
input-queue concepts. Redis still owns:

- Per-session output stream (``session_stream:{id}``) — worker XADDs
  axon events here, the SSE endpoint forwards to the browser.
- Per-user notification channel (``user_events:bus``) — cross-session
  status events, fanned out in-process to a user's SSE connections.
- Wake pub/sub (``notify:worker``) — backend pings after INSERTing a
  ``session_turn`` so the worker doesn't have to wait for its 5s poll.
  Best-effort: a missed publish is recovered by the poll fallback.
- Cancel pub/sub (``cancel:{session_id}``) — fire-and-forget, reaches
  the worker holding the session (if any).
"""

import asyncio
import uuid
from typing import Any, cast
from uuid import UUID

import redis.asyncio as aioredis

# Pause before treating a Redis ``ConnectionError`` as an empty block, so a
# persistently-unreachable Redis can't hot-spin the SSE follow loop (which
# would otherwise re-issue XREAD with no blocking delay). Short enough that
# a recovered Redis resumes streaming promptly.
_CONNECTION_ERROR_BACKOFF_SECONDS = 1.0

# Wire contract — **mirrored** in ``agent-worker/agent_worker/wire/keys.py``
# (``STREAM_PREFIX``, ``USER_EVENTS_CHANNEL``, ``NOTIFY_CHANNEL``). Any
# change here must be coordinated there, or the two services silently
# desync.

# Per-session output stream prefix. The worker XADDs events to
# ``session_stream:{session_id}``; this SSE endpoint XRANGE/XREADs from it.
SESSION_STREAM_PREFIX = "session_stream"

# Per-user notification channel. ``session.status_changed`` is PUBLISHed
# as a ``{user_id, event}`` JSON payload to this single global channel — by
# the worker for the terminals it writes, by the backend for the transitions
# it writes (``app/services/user/events.py``). Each backend process
# SUBSCRIBEs once and fans the event out in-process to that user's SSE
# connections (see ``app/services/user/hub.py``). One global channel — not one per
# user — so a process holds a single subscription no matter how many
# users it serves.
USER_EVENTS_CHANNEL = "user_events:bus"

# Pub/Sub channel the backend publishes to after INSERTing a
# ``session_turn`` row. Wakes idle workers immediately instead of
# making them wait for the 5s poll fallback.
WORKER_NOTIFY_CHANNEL = "notify:worker"

# Pub/Sub channel prefix for per-session cancel signals. Mirrored in
# ``agent-worker/agent_worker/runtime/cancellation.py``; fire-and-forget
# ``PUBLISH cancel:{session_id}`` reaches the worker holding that
# session (if any), which cancels the agent and stamps the payload as
# ``session_turn.termination_reason`` (a ``TerminationReason`` value:
# ``user_cancelled`` from the cancel endpoint, ``insufficient_credits``
# from the budget enforcer; empty/unknown falls back to
# ``user_cancelled``, so either side can deploy first).
CANCEL_CHANNEL_PREFIX = "cancel"

# Pub/Sub channel the worker publishes to after committing a terminal
# ``session_turn`` write (COMPLETE / CANCELLED / FAILED). Payload is the session_id
# string. Mirrored in ``agent-worker/agent_worker/wire/keys.py``. Named for
# its first subscriber, :mod:`app.workers.settle_subscriber`, which settles
# the session's reservation at once; :mod:`app.workers.message_queue_dispatcher`
# follows it too, to send a queued message on without waiting out its tick.
# Pub/sub is fire-and-forget on purpose — each subscriber is backstopped by
# a poll (``settle_sweeper``'s, the dispatcher's own) for any message lost
# to a disconnect.
SETTLE_NOTIFY_CHANNEL = "settle:notify"

# Per-email registration verification code prefix. Plain string keys
# (not streams), one per pending registration. JSON value carries the
# code + send timestamp + attempt counter; TTL controls the validity
# window. See :mod:`app.services.auth.verification`.
EMAIL_VERIFY_PREFIX = "email_verify"

# Per-(phone) registration/login verification code prefix. Same JSON
# shape as the email prefix; namespaced separately so phone and email
# codes for the same person can't collide.
PHONE_VERIFY_PREFIX = "phone_verify"

# Per-(new-email) email-change verification code prefix. Same JSON
# shape as the registration prefix, but namespaced separately so a
# pending registration can't be hijacked by an email-change request
# (or vice versa) targeting the same address.
EMAIL_CHANGE_PREFIX = "email_change"

# Per-(new-phone) phone-change verification code prefix. The SMS-channel
# sibling of ``EMAIL_CHANGE_PREFIX``; namespaced separately from
# registration/login phone codes for the same reason.
PHONE_CHANGE_PREFIX = "phone_change"

# Per-user sudo code prefix: a code sent to an address the account already
# has, standing in for the current password before a credential changes.
# Keyed by user rather than address, so whichever channel the latest code
# went to, it is the one that counts.
SUDO_CODE_PREFIX = "sudo_code"


# Per-``state`` pending OAuth-authorization state for MCP Connectors. Holds the
# PKCE code_verifier + connector_id + user_id for the redirect round-trip; the
# opaque random ``state`` is the key. One-shot: deleted on callback consume.
OAUTH_STATE_PREFIX = "connector_oauth_state"


def stream_key(prefix: str, entity_id: str | UUID) -> str:
    return f"{prefix}:{entity_id}"


def oauth_state_key(state: str) -> str:
    """Redis key holding the pending OAuth-authorization state for ``state``."""
    return f"{OAUTH_STATE_PREFIX}:{state}"


def oauth_current_state_key(connector_id: int) -> str:
    """Redis key naming the one authorization a Connector is currently waiting on.

    Each ``state`` is independently valid for its whole TTL, so without this a
    user who starts a second authorization — a second tab, a different account,
    a first attempt they thought had failed — leaves two live flows for one
    Connector, and whichever *finishes* last wins. This records which one they
    started last, which is the one they meant.
    """
    return f"{OAUTH_STATE_PREFIX}:current:{connector_id}"


def email_verify_key(email: str) -> str:
    """Redis key holding the active registration verification state
    for ``email``. Lowercased so casing variations of the same address
    can't be used to bypass the resend cooldown."""
    return f"{EMAIL_VERIFY_PREFIX}:{email.lower()}"


def phone_verify_key(phone: str) -> str:
    """Redis key holding the active registration/login verification
    state for ``phone``."""
    return f"{PHONE_VERIFY_PREFIX}:{phone}"


def email_change_key(user_id: int, email: str) -> str:
    """Redis key holding the pending email-change verification state for
    ``user_id`` switching to the *new* address. Bound to the requesting
    user (not the address alone) so two users targeting the same new
    address can't consume each other's code. Lowercased for the same
    reason as :func:`email_verify_key`."""
    return f"{EMAIL_CHANGE_PREFIX}:{user_id}:{email.lower()}"


def phone_change_key(user_id: int, phone: str) -> str:
    """Redis key holding the pending phone-change verification state for
    ``user_id`` switching to the *new* number. User-bound for the same
    reason as :func:`email_change_key`."""
    return f"{PHONE_CHANGE_PREFIX}:{user_id}:{phone}"


def sudo_code_key(user_id: int) -> str:
    """Redis key holding ``user_id``'s pending sudo code."""
    return f"{SUDO_CODE_PREFIX}:{user_id}"


def cancel_channel(session_id: str) -> str:
    """Pub/Sub channel for cancelling a specific session."""
    return f"{CANCEL_CHANNEL_PREFIX}:{session_id}"


async def publish_wake(
    redis: aioredis.Redis,
    session_id: uuid.UUID,
) -> None:
    """Notify workers that a new ``session_turn`` row is queued.

    Best-effort fast-path: workers also poll ``claim_next_turn`` every
    few seconds (see ``settings.claim_poll_interval_seconds`` on the
    worker side), so a publish failure or pub/sub disconnect just adds
    one poll cycle of latency — never strands the run.

    Caller invokes **after** the DB transaction that INSERTed the
    ``session_turn`` has committed; otherwise a worker waking on the
    pub/sub could ``claim_next_turn`` before our row is visible and
    re-sleep, defeating the wake's purpose.
    """
    await redis.publish(WORKER_NOTIFY_CHANNEL, str(session_id))


async def read_stream(
    redis: aioredis.Redis,
    key: str,
    cursor: str = "0",
    *,
    count: int = 50,
    block_ms: int = 15_000,
) -> list:
    """Block-read up to ``count`` new entries.

    ``block_ms`` is the **upper bound** on how long XREAD waits before
    returning empty — Redis returns immediately the moment a new entry
    is XADDed, so a long block does not delay live events. The 15s
    default just keeps the calling generator from spinning when the
    stream is idle (10 idle SSE clients × 100ms loop = 100 RPS of
    pointless XREAD traffic).

    A socket read timeout is treated as "no new entries" (return ``[]``)
    rather than propagated: a blocking XREAD that trips the connection's
    read timeout — a transient network blip, or a ``socket_timeout``
    shorter than ``block_ms`` — should make the SSE generator loop, not
    crash the stream. The caller advances no cursor and re-blocks.

    A ``ConnectionError`` is handled the same way (empty block), not
    propagated: the pooled client already retried internally, and tearing
    the generator down on a blip would drop **every** live SSE at once →
    a reconnect herd against the fail-fast streaming pool. We pause briefly
    so a persistently-down Redis doesn't hot-spin, then return ``[]``; the
    caller's DB-terminal fallback still closes finished sessions, and a
    still-running session resumes streaming once Redis recovers.
    """
    try:
        # ``xread`` is typed as a union over every decode / grouping mode;
        # this client's is the list form.
        entries = await redis.xread({key: cursor}, count=count, block=block_ms)
        return cast("list[Any]", entries) or []
    except aioredis.TimeoutError:
        return []
    except aioredis.ConnectionError:
        await asyncio.sleep(_CONNECTION_ERROR_BACKOFF_SECONDS)
        return []
