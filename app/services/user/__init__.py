"""Non-trivial user-scoped service logic, split by concern.

Public API is re-exported here so callers can write
``from app.services.user import stream_user_events`` without knowing
which submodule the function lives in.

Submodule responsibilities:

- :mod:`~app.services.user.hub` — :class:`UserEventHub`, the process-wide
  in-process fan-out for the per-user notification channel.
- :mod:`~app.services.user.events` — SSE generator that tails one user's
  notifications off the hub.
"""

from app.services.user.events import stream_user_events
from app.services.user.hub import UserEventHub

__all__ = ["UserEventHub", "stream_user_events"]
