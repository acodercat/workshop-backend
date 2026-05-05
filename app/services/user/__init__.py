"""Non-trivial user-scoped service logic, split by concern.

Public API is re-exported here so callers can write
``from app.services.user import stream_user_events`` without knowing
which submodule the function lives in.

Submodule responsibilities:

- :mod:`~app.services.user.events` — SSE generator for the per-user
  notification stream (``user_events:{user_id}``).
"""

from app.services.user.events import stream_user_events

__all__ = ["stream_user_events"]
