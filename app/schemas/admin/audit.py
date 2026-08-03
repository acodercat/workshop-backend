"""Response model for the audit trail.

Read-only: rows are written exclusively by
:func:`app.services.admin.audit`, and the table is append-only, so there
is no create or update shape to model here.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditLogResponse(BaseModel):
    """One recorded operator action.

    ``staff_username`` is joined in rather than left to the reader to
    resolve from ``staff_id``: the log is read by people asking "who did
    this", and an id alone answers that only for whoever holds the
    directory. The id stays too, because usernames are the account's
    login key and a future rename must not silently re-attribute
    history.
    """

    id: int
    staff_id: int
    staff_username: str
    action: str
    target_type: str | None
    target_id: str | None
    reason: str | None
    payload: dict[str, Any]
    request_id: str | None
    ip_address: str | None
    created_at: datetime
