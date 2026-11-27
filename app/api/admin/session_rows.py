"""The rows of the console's two session lists.

``GET /sessions`` and ``GET /users/{id}/sessions`` show the same session row
and differ by the owner columns alone (see :mod:`app.api.admin.session`), so
the row is built here once and the platform-wide list adds its owner to it.
"""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import Session
from app.schemas.admin.session import SessionResponse
from app.services import session as session_service
from app.services.credits import analytics
from app.services.credits.rates import credits_from_micro


async def session_responses(db: AsyncSession, sessions: Sequence[Session]) -> list[SessionResponse]:
    """*sessions* as response rows, in order, with the usage aggregated for
    the whole page in two queries, not two per row, each asked of the module
    that owns the table it reads: usage of the session domain, money of the
    ledger's (see ``session_spend`` for why only a negative SETTLE counts as
    spend).

    A session that has run nothing is absent from both aggregates by design,
    so both are read through a default rather than indexed.
    """
    session_ids = [session.id for session in sessions]
    turns = await session_service.per_session_usage(db, session_ids)
    spend_micro = await analytics.session_spend(db, session_ids)
    responses = []
    for session in sessions:
        usage = turns.get(session.id, session_service.NO_TURN_USAGE)
        responses.append(
            SessionResponse(
                id=session.id,
                title=session.title,
                status=session.status,
                agent_type=session.agent_type,
                model_id=session.model_id,
                turn_count=usage.turn_count,
                total_tokens=usage.total_tokens,
                spent_credits=credits_from_micro(spend_micro.get(session.id, 0)),
                created_at=session.created_at,
                updated_at=session.updated_at,
                deleted_at=session.deleted_at,
            )
        )
    return responses
