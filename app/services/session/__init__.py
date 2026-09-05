"""Non-trivial session logic, split by concern.

Public API is re-exported here so callers can write
``from app.services.session import queue_user_message, stream_events,
generate_title`` without knowing which submodule the function lives in.

Submodule responsibilities:

- :mod:`~app.services.session.enqueue` — user-message persist + enqueue.
- :mod:`~app.services.session.streaming` — SSE replay-then-follow generator.
- :mod:`~app.services.session.title` — LLM-backed title generation.
- :mod:`~app.services.session.recovery` — backend-side terminal writes
  (abandoned-session sweep + user-cancel of an unclaimed turn).
- :mod:`~app.services.session.stats` — turn and token aggregates over
  ``session_turn``, for the console's dashboard and session lists.

Shared DB-query helpers for endpoint handlers live alongside the
endpoint file itself — they build HTTP concerns (``HTTPException``,
Pydantic response models) and have no callers outside the endpoint layer.
"""

from app.services.session.enqueue import queue_user_message
from app.services.session.recovery import cancel_unclaimed_turn
from app.services.session.stats import (
    NO_TURN_USAGE,
    TurnUsage,
    count_tokens_since,
    per_session_usage,
)
from app.services.session.streaming import stream_events
from app.services.session.title import generate_title

__all__ = [
    "NO_TURN_USAGE",
    "TurnUsage",
    "cancel_unclaimed_turn",
    "count_tokens_since",
    "generate_title",
    "per_session_usage",
    "queue_user_message",
    "stream_events",
]
