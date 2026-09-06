"""The console's platform-wide session list.

- ``GET /sessions`` — every session, newest activity first, soft-deleted
  included, with the owner on the row.

**There is exactly one route here, and no ``GET /sessions/{id}``.** A
conversation is the most private thing the platform holds, and no
question this surface exists to answer needs one — so rather than
auditing the read, the read does not exist. What the row carries instead
is usage: ``turn_count`` / ``total_tokens`` / ``spent_credits`` answer
"was this session expensive, and why" without anyone reading a word the
user wrote.

The per-user view of the same rows lives in :mod:`app.api.admin.user`
(``GET /users/{id}/sessions``). This one differs by the owner column and
nothing else — same ordering, same page size, same soft-delete policy,
same usage columns — so an operator who learns one list can read the
other.
"""

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.admin.dependencies import StaffContext
from app.api.admin.window import OptionalWindowDays
from app.common.business_time import window_start
from app.common.pagination import paginate
from app.models.session import Session
from app.models.user import User
from app.schemas.admin.session import SessionWithOwnerResponse
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.services import session as session_service
from app.services.credits import analytics
from app.services.credits.rates import credits_from_micro

router = APIRouter(tags=["sessions"])


@router.get("/sessions", response_model=PaginatedResponse[SessionWithOwnerResponse])
async def list_sessions(
    ctx: StaffContext,
    days: OptionalWindowDays,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[SessionWithOwnerResponse]:
    """Return one page of every session on the platform, most recently
    active first — **including soft-deleted ones**, ``deleted_at``
    visible. The customer list hides those; support is often asked about
    exactly them.

    ``days`` cuts on ``updated_at``, the column this list is sorted by
    and the one it shows: filtering on anything else would order rows by
    a date the operator cannot see and hide rows whose visible date is
    inside the window. It also costs nothing —
    ``ix_session_updated_at`` leads on that column in this direction, so
    the filter becomes a range on the index the sort already reads.
    (The conversation *statistics* cut on ``created_at`` instead: they
    count conversations started, not touched.)
    """
    filters = []
    if days is not None:
        filters.append(Session.updated_at >= window_start(days))

    # Counted under the same filters as the page: a total that answered
    # a wider question than the rows beneath it would put a page count on
    # a list that cannot fill it. No join — nothing filtered here lives
    # on the owner.
    total = await ctx.db.scalar(select(func.count()).select_from(Session).where(*filters))
    # The owner is joined rather than resolved per row, the same shape
    # the audit log uses for its operator: the list is read as "whose
    # session is this", and a page of rows must not become a page of
    # lookups. Both name columns come off the one join — the console
    # shows a username with the display name under it, and fetching the
    # second separately would be the per-row lookup this avoids. Outer,
    # because ``session.user_id`` is nullable — an inner join would
    # silently drop such a row from a page whose ``total`` still counted
    # it.
    #
    # ``id`` breaks ties on ``updated_at`` so a row cannot appear on two
    # pages or none: OFFSET paging over a non-deterministic sort is free
    # to reorder equal keys between requests. ``ix_session_updated_at``
    # is keyed on both, in this direction, so the sort is read off the
    # index rather than done.
    rows = (
        await ctx.db.execute(
            paginate(
                select(Session, User.username, User.name)
                .outerjoin(User, User.id == Session.user_id)
                .where(*filters)
                .order_by(Session.updated_at.desc(), Session.id.desc()),
                page,
                page_size,
            )
        )
    ).all()

    session_ids = [session.id for session, _, _ in rows]
    turns = await session_service.per_session_usage(ctx.db, session_ids)
    spend_micro = await analytics.session_spend(ctx.db, session_ids)

    # A session that has run nothing is absent from both aggregates by
    # design, so both are read through a default rather than indexed.
    return PaginatedResponse(
        items=[
            SessionWithOwnerResponse(
                id=session.id,
                title=session.title,
                status=session.status,
                agent_type=session.agent_type,
                model_id=session.model_id,
                turn_count=turns.get(session.id, session_service.NO_TURN_USAGE).turn_count,
                total_tokens=turns.get(session.id, session_service.NO_TURN_USAGE).total_tokens,
                spent_credits=credits_from_micro(spend_micro.get(session.id, 0)),
                created_at=session.created_at,
                updated_at=session.updated_at,
                deleted_at=session.deleted_at,
                user_id=session.user_id,
                owner_username=owner_username,
                owner_name=owner_name,
            )
            for session, owner_username, owner_name in rows
        ],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )
