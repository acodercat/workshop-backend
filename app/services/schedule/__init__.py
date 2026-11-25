"""Non-trivial scheduled-task logic, split by concern.

Public API is re-exported here so callers can write
``from app.services.schedule import next_run_after`` without knowing
which submodule the function lives in — same arrangement as
:mod:`app.services.session`.

Submodule responsibilities:

- :mod:`~app.services.schedule.recurrence` — when a schedule fires next,
  and how late an occurrence may be delivered. Pure functions over a
  ``Schedule`` row; no I/O.
- :mod:`~app.services.schedule.lifecycle` — what an owner's edit does to a
  schedule's status and clock. Pure too; the caller holds the row lock.
- :mod:`~app.services.schedule.briefing` — what the agent is told about the
  scheduled task it is running. Pure too.
- :mod:`~app.services.schedule.connectors` — reading and writing which
  Connectors a schedule hands to the sessions it creates.

``lifecycle`` and ``connectors`` are not re-exported: their functions read
as ``schedule_connectors.replace(...)`` at the call sites,
which is how the session package's twins are used and what keeps each
distinct from the session-level call sitting next to it in the dispatcher.
"""

from app.services.schedule.briefing import render_run_reminder
from app.services.schedule.recurrence import catch_up_grace, next_run_after

__all__ = [
    "catch_up_grace",
    "next_run_after",
    "render_run_reminder",
]
