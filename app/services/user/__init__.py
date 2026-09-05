"""Non-trivial user-scoped service logic, split by concern.

Public API is re-exported here so callers can write
``from app.services.user import stream_user_events`` without knowing
which submodule the function lives in.

Submodule responsibilities:

- :mod:`~app.services.user.hub` — :class:`UserEventHub`, the process-wide
  in-process fan-out for the per-user notification channel.
- :mod:`~app.services.user.events` — SSE generator that tails one user's
  notifications off the hub.
- :mod:`~app.services.user.stats` — aggregate counts over the account
  table, for the console dashboard.
- :mod:`~app.services.user.identity` — the username / display-name pair
  the console renders for a row's owner.
"""

from app.services.user.events import stream_user_events
from app.services.user.hub import UserEventHub
from app.services.user.identity import owner_identity
from app.services.user.stats import count_registered_since

__all__ = [
    "UserEventHub",
    "count_registered_since",
    "owner_identity",
    "stream_user_events",
]
