"""The one rule for changing a session's ``agent_type``.

A session only ever moves LITE → FULL. A downgrade would strand the
session's sandbox and files, and the UI never offers one, so a request
for it is a client programming error.

Extracted from :mod:`~app.services.session.enqueue` so every path that
changes the mode applies the same rule. Callers hold the session row lock.
"""

from app.errors import AgentTypeDowngradeError
from app.models.session import AgentType, Session


def upgrade_agent_type(session: Session, requested: AgentType | None) -> None:
    """Apply the one-way LITE → FULL upgrade, if any.

    ``None`` (no preference) and same-as-current are no-ops, so resends
    are idempotent. A FULL → LITE request raises
    :class:`~app.errors.AgentTypeDowngradeError`.
    """
    if requested is None or requested == session.agent_type:
        return
    if requested is not AgentType.FULL:
        raise AgentTypeDowngradeError("A session can only switch from LITE to FULL")
    session.agent_type = requested
