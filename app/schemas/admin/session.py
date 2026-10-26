"""Response models for the console's two session lists — the platform-wide
one (:mod:`app.api.admin.session`) and a single user's
(:mod:`app.api.admin.user`).

**There is no content shape here, and that is the point.** The console
can say how much a session cost and how long it ran; it cannot open it.
A conversation is the most private thing the platform holds, and an
operator has no business reading one to answer the questions this surface
exists for. What replaced the content view is the usage on the row —
``turn_count`` / ``total_tokens`` / ``spent_credits`` answer "was this
session expensive, and why" without anyone reading a word the user wrote.

``deleted_at`` is visible where the customer list hides it: support is
often asked about exactly the sessions a user deleted.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.session import AgentType, SessionStatus


class SessionResponse(BaseModel):
    """One row of a user's session list, soft-deleted included.

    Not ``from_attributes``: the three usage fields are aggregated per
    page rather than read off the ORM row, so the endpoint builds this
    explicitly and nothing can silently ship a half-populated row.
    """

    id: UUID
    title: str | None
    status: SessionStatus
    agent_type: AgentType
    # What actually ran, not what was asked for. ``session.tier`` is the
    # capability the customer picked and is deliberately absent here:
    # it is resolved through ``settings.session_tier_models[tier]`` at
    # creation, and ``model_id`` is the result — the execution and
    # billing truth, and the only one of the two that explains a cost.
    model_id: str
    # Turns run, tokens burned, and credits actually settled. Zero means
    # "nothing measured yet" — a turn still running has an estimate held
    # against it, and an estimate does not belong in a column of actuals.
    turn_count: int
    total_tokens: int
    spent_credits: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class SessionWithOwnerResponse(SessionResponse):
    """One row of the platform-wide session list: the same session, plus
    who ran it.

    Its own model rather than optional fields on
    :class:`SessionResponse`: a user's own session list already knows
    whose it is, and carrying an owner there that is always the page's
    user would be a column of one repeated value. Kept as a subclass
    because the row genuinely is a session row with the owner added —
    the usage columns and their meaning are identical.

    ``owner_name`` rides along beside the username for the reason
    :class:`~app.schemas.admin.stats.UserActivityEntry` carries both: the
    console renders an operator-facing identity, and a username alone
    reads as an id to whoever knows the person by their display name.
    """

    user_id: int
    owner_username: str
    owner_name: str
