"""Dashboard aggregates over the credit ledger.

- ``GET /stats/credits``          — tiles, daily series, spend by model.
- ``GET /stats/top-spenders``     — the spend leaderboard.
- ``GET /stats/recent-users``     — the same rows, most recent first.
- ``GET /stats/check-in-leaders`` — the check-in leaderboard.
- ``GET /stats/recent-check-ins`` — the same rows, newest first.
- ``GET /stats/check-ins``        — daily-check-in engagement.
- ``GET /stats/users``            — new accounts in the window.
- ``GET /stats/tokens``           — tokens burned in the window.

Under ``/stats``, not ``/metrics``: the ASGI factory already serves
Prometheus at ``/metrics`` on both apps.

**Not audited**, like every read here: the console has no audited read
because it has no way to read one customer's content at all. These are
counts, never a conversation.

**No aggregation lives in this module.** Each figure is computed by
whichever domain owns the table it comes from — the ledger's four by
:mod:`app.services.credits.analytics`, check-ins by
:mod:`app.services.check_in`, registrations by
:mod:`app.services.user.stats`, tokens by
:mod:`app.services.session.stats`. Folding any of them into
``/stats/credits`` would make the credits module answer for a table it
does not own — which is why this module is a row of narrow endpoints
rather than one fat one, and why a new figure arrives as another
endpoint rather than another field.
"""

from fastapi import APIRouter, Query

from app.api.admin.dependencies import StaffContext
from app.api.admin.window import WindowDays
from app.common.business_time import window_start
from app.schemas.admin.stats import (
    CheckInActivityEntry,
    CheckInStatsResponse,
    CreditsStatsResponse,
    DailySpendPoint,
    ModelSpendEntry,
    TokenStatsResponse,
    UserActivityEntry,
    UserStatsResponse,
)
from app.services import check_in, session as session_service, user as user_service
from app.services.credits import analytics
from app.services.credits.analytics import UserActivityOrder
from app.services.credits.rates import credits_from_micro

router = APIRouter(tags=["stats"])


@router.get("/stats/credits", response_model=CreditsStatsResponse)
async def read_credits_stats(
    ctx: StaffContext,
    days: WindowDays,
) -> CreditsStatsResponse:
    """Return the dashboard's headline figures for the trailing window."""
    summary = await analytics.summarize(ctx.db, days=days)
    return CreditsStatsResponse(
        days=days,
        spend_credits=credits_from_micro(summary.spend_micro),
        granted_credits=credits_from_micro(summary.granted_micro),
        granted_free_credits=credits_from_micro(summary.granted_free_micro),
        clawed_back_credits=credits_from_micro(summary.clawed_back_micro),
        active_users=summary.active_users,
        settled_turns=summary.settled_turns,
        outstanding_credits=credits_from_micro(summary.outstanding_micro),
        daily=[
            DailySpendPoint(day=point.day, spend_credits=credits_from_micro(point.spend_micro))
            for point in summary.daily
        ],
        by_model=[
            ModelSpendEntry(
                model_id=entry.model_id,
                spend_credits=credits_from_micro(entry.spend_micro),
            )
            for entry in summary.by_model
        ],
    )


@router.get("/stats/top-spenders", response_model=list[UserActivityEntry])
async def read_top_spenders(
    ctx: StaffContext,
    days: WindowDays,
    limit: int = Query(20, ge=1, le=100),
) -> list[UserActivityEntry]:
    """Return the heaviest consumers over the window, biggest first.

    A bare list, not a ``PaginatedResponse``: a leaderboard is a top-N
    by definition — paging into the tail is what the users list is for.
    """
    return await _read_user_activity(ctx, days=days, limit=limit, order=UserActivityOrder.SPEND)


@router.get("/stats/recent-users", response_model=list[UserActivityEntry])
async def read_recent_users(
    ctx: StaffContext,
    days: WindowDays,
    limit: int = Query(20, ge=1, le=100),
) -> list[UserActivityEntry]:
    """Return the users who spent most recently, latest first.

    "Active" means *spent* here, matching the dashboard's own tile —
    a login without usage costs nothing and doesn't belong on a credits
    view.
    """
    return await _read_user_activity(ctx, days=days, limit=limit, order=UserActivityOrder.RECENT)


async def _read_user_activity(
    ctx: StaffContext,
    *,
    days: int,
    limit: int,
    order: UserActivityOrder,
) -> list[UserActivityEntry]:
    """Shared body of the two rankings — same rows, same conversion,
    different ordering."""
    rows = await analytics.user_activity(ctx.db, days=days, limit=limit, order=order)
    return [
        UserActivityEntry(
            user_id=row.user_id,
            username=row.username,
            name=row.name,
            spend_credits=credits_from_micro(row.spend_micro),
            settled_turns=row.settled_turns,
            balance_credits=credits_from_micro(row.balance_micro),
            last_spend_at=row.last_spend_at,
        )
        for row in rows
    ]


@router.get("/stats/check-in-leaders", response_model=list[CheckInActivityEntry])
async def read_check_in_leaders(
    ctx: StaffContext,
    days: WindowDays,
    limit: int = Query(20, ge=1, le=100),
) -> list[CheckInActivityEntry]:
    """Users ranked by how many days they checked in over the window."""
    return await _check_in_activity(
        ctx, days=days, limit=limit, order=check_in.CheckInRankOrder.COUNT
    )


@router.get("/stats/recent-check-ins", response_model=list[CheckInActivityEntry])
async def read_recent_check_ins(
    ctx: StaffContext,
    days: WindowDays,
    limit: int = Query(20, ge=1, le=100),
) -> list[CheckInActivityEntry]:
    """The same users, newest check-in first — who came back today."""
    return await _check_in_activity(
        ctx, days=days, limit=limit, order=check_in.CheckInRankOrder.RECENT
    )


async def _check_in_activity(
    ctx: StaffContext,
    *,
    days: int,
    limit: int,
    order: check_in.CheckInRankOrder,
) -> list[CheckInActivityEntry]:
    """Shared body of the two check-in rankings — same rows, different
    ordering, mirroring the spend pair above."""
    rows = await check_in.user_activity(ctx.db, since=window_start(days), limit=limit, order=order)
    return [
        CheckInActivityEntry(
            user_id=row.user_id,
            username=row.username,
            name=row.name,
            check_ins=row.check_ins,
            streak_day=row.streak_day,
            last_check_in_at=row.last_check_in_at,
        )
        for row in rows
    ]


@router.get("/stats/check-ins", response_model=CheckInStatsResponse)
async def read_check_in_stats(ctx: StaffContext, days: WindowDays) -> CheckInStatsResponse:
    """Return check-in engagement for the window.

    Its own endpoint rather than a field on ``/stats/credits``: the
    aggregation belongs to :mod:`app.services.check_in`, and folding a
    non-ledger figure into the ledger's response would make that module
    answer for a table it does not own.
    """
    check_ins, users = await check_in.count_in_window(ctx.db, since=window_start(days))
    return CheckInStatsResponse(days=days, check_ins=check_ins, users=users)


@router.get("/stats/users", response_model=UserStatsResponse)
async def read_user_stats(ctx: StaffContext, days: WindowDays) -> UserStatsResponse:
    """Return how many accounts were created in the window.

    Its own endpoint for the same reason the check-in one is: the count
    comes off the ``user`` table, which :mod:`app.services.user.stats`
    owns, and the ledger's response has no business carrying it.
    """
    new_users = await user_service.count_registered_since(ctx.db, since=window_start(days))
    return UserStatsResponse(days=days, new_users=new_users)


@router.get("/stats/tokens", response_model=TokenStatsResponse)
async def read_token_stats(ctx: StaffContext, days: WindowDays) -> TokenStatsResponse:
    """Return tokens burned in the window.

    Its own endpoint for the same reason the check-in and user ones are:
    the counts come off ``session_turn``, which
    :mod:`app.services.session.stats` owns.
    """
    input_tokens, output_tokens = await session_service.count_tokens_since(
        ctx.db, since=window_start(days)
    )
    return TokenStatsResponse(days=days, input_tokens=input_tokens, output_tokens=output_tokens)
