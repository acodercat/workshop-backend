"""Scheduled-task CRUD and run history.

A schedule is a standing instruction to run one agent turn on a clock. This
surface only manages the definition; firing it is
``app/workers/schedule_dispatcher.py``, and the turn it produces is an
ordinary session the rest of the API already knows how to serve.

``next_run_at`` is never accepted from a caller — it is derived from the
recurrence by :mod:`app.services.schedule.lifecycle` whenever the recurrence
changes or the schedule is activated, so the dispatcher's scan column and the
spec the owner sees can never disagree.

An occurrence's outcome is the **first turn** of the session it created, and
every read here — the history, the card's latest run, the unread badge —
joins it through :func:`_with_outcome`, so they cannot answer differently.
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import (
    ColumnElement,
    Row,
    Select,
    and_,
    func,
    or_,
    select,
    true,
    update as sql_update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.common.pagination import paginate
from app.errors import ScheduleActiveLimitError, ScheduleLimitError
from app.models.project import Project
from app.models.schedule import Schedule, ScheduleRun, ScheduleStatus
from app.models.session import AgentType, Session
from app.models.session_turn import SessionTurn, SessionTurnStatus, TerminationReason
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.schemas.schedule import (
    UNREAD_ACKNOWLEDGE_MAX,
    MarkRunsReadRequest,
    ScheduleCreate,
    ScheduleListResponse,
    ScheduleResponse,
    ScheduleRunResponse,
    ScheduleUpdate,
    UnreadCountResponse,
)
from app.services.connector import ownership as connector_ownership
from app.services.project import ownership as project_ownership
from app.services.schedule import connectors as schedule_connectors, lifecycle
from app.services.session.creation import resolve_tier

router = APIRouter(tags=["schedules"])

# Per-user cap on *rows*, distinct from the cap on ACTIVE ones. A one-shot
# frees its ACTIVE slot the moment it fires, so ``_MAX_ACTIVE_SCHEDULES``
# bounds standing spend but not how many rows accumulate — without this a
# user creating a one-shot a day would grow the unpaginated list endpoint
# without limit.
_MAX_SCHEDULES_PER_USER = 100

# Per-user cap on *active* schedules. Bounds standing spend: every active
# schedule bills a turn each time it fires. Paused and completed rows are
# uncapped by this one — a user may keep as many as they like as long as
# they are not firing.
_MAX_ACTIVE_SCHEDULES = 5

# Turn states with an outcome to report. A turn still PENDING or RUNNING is
# not news yet — badging it would turn the unread count into a progress
# indicator.
_TURN_ENDED = (
    SessionTurnStatus.COMPLETE,
    SessionTurnStatus.FAILED,
    SessionTurnStatus.CANCELLED,
)


async def _get_or_404(
    db: AsyncSession, schedule_id: int, user_id: int, *, for_update: bool = False
) -> Schedule:
    """Load a schedule the caller owns.

    Ownership is folded into the lookup rather than checked after it, so
    somebody else's schedule and a nonexistent one are the same
    indistinguishable 404 and ids don't leak.

    ``for_update`` takes the row lock the dispatcher settles under. An edit
    needs it before deciding anything: see :mod:`app.services.schedule.lifecycle`.
    """
    query = select(Schedule).where(Schedule.id == schedule_id, Schedule.user_id == user_id)
    if for_update:
        query = query.with_for_update()
    schedule = (await db.execute(query)).scalar_one_or_none()
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="schedule not found")
    return schedule


async def _assert_row_headroom(db: AsyncSession, user_id: int) -> None:
    """Refuse a schedule the caller has no room to store.

    Race-acceptable, like the active cap: two concurrent creates could each
    pass and put the user one over, which is not worth a row lock for a
    soft policy limit.
    """
    total = await db.scalar(select(func.count(Schedule.id)).where(Schedule.user_id == user_id))
    if total is not None and total >= _MAX_SCHEDULES_PER_USER:
        raise ScheduleLimitError(_MAX_SCHEDULES_PER_USER)


async def _assert_activation_headroom(
    db: AsyncSession, user_id: int, *, excluding: int | None = None
) -> None:
    """Refuse one ACTIVE schedule more than the cap allows.

    ``excluding`` is an existing schedule being turned on: it must not count
    against itself, and its status may already read ACTIVE in this
    transaction by the time the count runs.

    Only ACTIVE rows count: an active schedule bills a turn every time it
    fires, which is the thing worth capping. A user may keep any number of
    paused ones. Race-acceptable — two concurrent activations could each
    pass and put the user one over, which is not worth a row lock for a soft
    policy limit.
    """
    query = select(func.count(Schedule.id)).where(
        Schedule.user_id == user_id, Schedule.status == ScheduleStatus.ACTIVE
    )
    if excluding is not None:
        query = query.where(Schedule.id != excluding)
    active = await db.scalar(query)
    if active is not None and active >= _MAX_ACTIVE_SCHEDULES:
        raise ScheduleActiveLimitError(_MAX_ACTIVE_SCHEDULES)


# --- Occurrences ------------------------------------------------------------

# The turn the *dispatcher* enqueued — the first one, by id. A scheduled
# session is an ordinary session once it exists: nothing in the message
# endpoint knows about schedules, so its owner can keep prompting it and
# sessions run hundreds of turns deep. ``order_by`` + ``limit(1)`` is what
# picks the scheduled run out of everything that followed it.
_first_turn = (
    select(
        SessionTurn.status.label("status"),
        SessionTurn.termination_reason.label("termination_reason"),
        SessionTurn.created_at.label("started_at"),
        SessionTurn.terminated_at.label("ended_at"),
    )
    .where(SessionTurn.session_id == ScheduleRun.session_id)
    .order_by(SessionTurn.id.asc())
    .limit(1)
    .lateral("first_turn")
)


def _with_outcome(query: Select) -> Select:
    """*query* over ``schedule_run``, joined to what decides each outcome:
    the session it created, and that session's first turn.

    The one definition of an occurrence's outcome — the history, the card's
    latest run and the unread badge all build on it, so they cannot join it
    differently. Both joins are outer: a skipped occurrence has neither.
    """
    return query.join(Session, Session.id == ScheduleRun.session_id, isouter=True).outerjoin(
        _first_turn, true()
    )


def _runs(condition: ColumnElement[bool]) -> Select:
    """Occurrences matching *condition*, with the columns
    :func:`_run_response` reads. Unordered; each caller orders for its own
    purpose."""
    return _with_outcome(
        select(
            ScheduleRun,
            Session.deleted_at.label("session_deleted_at"),
            _first_turn.c.status,
            _first_turn.c.termination_reason,
            _first_turn.c.started_at,
            _first_turn.c.ended_at,
        )
    ).where(condition)


def _duration(started_at: datetime | None, ended_at: datetime | None) -> float | None:
    """How long the turn took, or ``None`` when that cannot be answered.

    Negative is refused rather than clamped to zero: the two timestamps are
    ``func.now()`` on whichever host wrote each, so a backwards step between
    them is a clock disagreement and not a run that took no time. "Unknown"
    is the honest answer, and it renders as nothing — same as an in-flight
    turn, which has no end yet.

    Note this measures from *enqueue*, not from claim: a turn that waited
    behind a busy fleet reports the wait as part of its duration. There is no
    claim-time column to subtract (``updated_at`` is bumped by heartbeats),
    so a schedule that suddenly takes six minutes may have queued for five.
    """
    if started_at is None or ended_at is None:
        return None
    seconds = (ended_at - started_at).total_seconds()
    return seconds if seconds >= 0 else None


def _run_response(row: Row[Any]) -> ScheduleRunResponse:
    """One row of :func:`_runs`, as the client renders it.

    Read by label rather than position: ``started_at`` and ``ended_at`` are
    both ``datetime | None``, so swapping them in the ``select()`` would
    type-check, run, and quietly compute a negative duration.

    ``termination_reason`` is dropped only when it is ``completed``, which
    says nothing ``turn_status`` has not already said.
    """
    run: ScheduleRun = row.ScheduleRun
    reason: TerminationReason | None = row.termination_reason
    return ScheduleRunResponse(
        id=run.id,
        scheduled_for=run.scheduled_for,
        session_id=(
            str(run.session_id)
            if run.session_id is not None and row.session_deleted_at is None
            else None
        ),
        turn_status=row.status,
        skip_reason=run.skip_reason,
        termination_reason=reason if reason is not TerminationReason.COMPLETED else None,
        duration_seconds=_duration(row.started_at, row.ended_at),
        read_at=run.read_at,
        created_at=run.created_at,
    )


def _unread(user_id: int) -> Select[tuple[int]]:
    """Ids of the caller's occurrences that are news and have not been read.

    **The badge counts this and the listing acknowledges this**, and they
    have to agree: a listing that acknowledged more than the badge counts
    would mark an occurrence whose turn is still running, and once it
    finished the badge would never mention it — the one outcome the user was
    waiting for, silently swallowed.

    News means an outcome to report: it was skipped, or its first turn has
    ended. A session the owner has since deleted is not news — they closed
    that book.
    """
    condition = and_(
        ScheduleRun.user_id == user_id,
        ScheduleRun.read_at.is_(None),
        or_(
            ScheduleRun.skip_reason.is_not(None),
            and_(
                Session.deleted_at.is_(None),
                _first_turn.c.status.in_(_TURN_ENDED),
            ),
        ),
    )
    return _with_outcome(select(ScheduleRun.id)).where(condition)


# --- Schedules --------------------------------------------------------------


async def _schedule_responses(
    db: AsyncSession, user_id: int, *, schedule_id: int | None = None
) -> list[ScheduleResponse]:
    """The caller's schedules as the management list renders them, newest
    first — or only *schedule_id*.

    The one place a ``ScheduleResponse`` is assembled. Create and update read
    their row back through here rather than echoing what they wrote, so every
    response — whichever endpoint returns it — carries the same fields,
    computed the same way, from what is actually stored. ``populate_existing``
    is what makes that true inside a request that has just written the row:
    this session does not expire on commit, so without it the identity map
    would hand back the object as the request left it.

    A schedule's project is reported only while it is live (see
    ``Schedule.project_id``), joined here so a deleted one reads as none.

    Four round trips however many rows: the schedules with their projects,
    their latest runs, then each side table batched by id.
    """
    query = (
        select(Schedule, Project.id.label("live_project_id"))
        .outerjoin(
            Project,
            and_(Project.id == Schedule.project_id, Project.deleted_at.is_(None)),
        )
        .where(Schedule.user_id == user_id)
        .order_by(Schedule.created_at.desc(), Schedule.id.desc())
        .execution_options(populate_existing=True)
    )
    if schedule_id is not None:
        query = query.where(Schedule.id == schedule_id)
    rows = list(await db.execute(query))
    schedules = [row.Schedule for row in rows]
    live_project_ids = {row.Schedule.id: row.live_project_id for row in rows}
    ids = [schedule.id for schedule in schedules]
    if not ids:
        return []
    latest = await db.execute(
        _runs(ScheduleRun.schedule_id.in_(ids))
        .distinct(ScheduleRun.schedule_id)
        .order_by(
            ScheduleRun.schedule_id,
            ScheduleRun.created_at.desc(),
            ScheduleRun.id.desc(),
        )
    )
    last_runs = {row.ScheduleRun.schedule_id: _run_response(row) for row in latest}
    connectors = await schedule_connectors.configured_ids_by_schedule(db, ids)
    return [
        ScheduleResponse.model_validate(schedule).model_copy(
            update={
                "last_run": last_runs.get(schedule.id),
                "connectors": connectors.get(schedule.id, []),
                "project_id": live_project_ids[schedule.id],
            }
        )
        for schedule in schedules
    ]


@router.post("/schedules", status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def create_schedule(
    request: Request,
    body: ScheduleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ScheduleResponse:
    """Create an ACTIVE schedule owned by the caller.

    403 if the caller is at either cap: too many rows, or too many active.
    """
    # Validate what the caller sent before asking whether they have room
    # for it: being told "you are at your limit" about a request that was
    # never going to be accepted costs a round trip and reads as the wrong
    # problem. The tier included: ``POST /sessions`` finds an unrunnable
    # tier the moment it builds the session, but a schedule builds hours
    # later, unattended — left unchecked, the fault would surface as three
    # ``misconfigured`` occurrences before the auto-pause caught it.
    resolve_tier(body.tier)
    schedule = Schedule(
        user_id=current_user.id,
        prompt=body.prompt,
        status=ScheduleStatus.ACTIVE,
        tier=body.tier,
        thinking_enabled=body.thinking_enabled,
        # Not exposed by the create form: a scheduled task runs the sandbox
        # agent. A stored column rather than a constant at the dispatch site
        # because a Session cannot be built without it, and surfacing it
        # later must not need a migration.
        agent_type=AgentType.FULL,
        # Names as given, order preserved: the worker's reminder lists them
        # the way the owner picked. Nothing is resolved here — a name is
        # checked against the live skill table at every firing, not once at
        # creation, so a schedule can name a skill uploaded later.
        skills=body.skills,
        project_id=body.project_id,
    )
    lifecycle.assign_recurrence(schedule, body.recurrence)
    lifecycle.arm(schedule, datetime.now(UTC))
    connector_ids = await connector_ownership.require_owned_connectors(
        db, user_id=current_user.id, ids=body.connectors
    )
    if body.project_id is not None:
        # Unlocked: a schedule is not something a project holds, and one
        # left naming a project deleted meanwhile simply runs in none.
        await project_ownership.require_owned_project(
            db, user_id=current_user.id, project_id=body.project_id
        )
    await _assert_row_headroom(db, current_user.id)
    await _assert_activation_headroom(db, current_user.id)
    db.add(schedule)
    # ``schedule.id`` is needed to bind the Connectors, and both must land in
    # one commit — a schedule that exists without the Connectors it was
    # created with would fire wrong until noticed.
    await db.flush()
    await schedule_connectors.replace(db, schedule.id, connector_ids)
    await db.commit()
    (response,) = await _schedule_responses(db, current_user.id, schedule_id=schedule.id)
    return response


@router.get("/schedules")
async def list_schedules(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ScheduleListResponse:
    """The caller's schedules, newest first, and the settled unread results the
    request observed.

    Unpaginated, which is only safe because ``_MAX_SCHEDULES_PER_USER``
    caps the rows — the ACTIVE cap alone would not, since a one-shot frees
    its slot as soon as it fires.

    **The unread ids are read before the schedules, and the order is the
    guarantee.** Each statement sees what had committed when it started, so
    anything the first saw settled, the second sees settled too: no id handed
    back belongs to a run a card in the same response renders as still
    running. A result settling between the two may be rendered settled
    without being acknowledged, and stays unread — the harmless direction.
    The other order would acknowledge results the page rendered as running.
    """
    unread_run_ids = list(
        (
            await db.execute(
                _unread(current_user.id).order_by(ScheduleRun.id).limit(UNREAD_ACKNOWLEDGE_MAX)
            )
        ).scalars()
    )
    return ScheduleListResponse(
        items=await _schedule_responses(db, current_user.id),
        unread_run_ids=unread_run_ids,
    )


@router.patch("/schedules/{schedule_id}")
@limiter.limit("30/minute")
async def update_schedule(
    request: Request,
    schedule_id: int,
    body: ScheduleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ScheduleResponse:
    """Edit, pause, or resume a schedule, or move it into a project or out
    of one.

    ``None`` leaves a field alone; ``[]`` clears a list. What the edit does to
    the status and the clock is :func:`lifecycle.apply_owner_edit`'s to
    decide, on the row locked here for the rest of the transaction — which
    also serialises two concurrent edits of one schedule.
    """
    # Validated before the lock is taken, and before the caps are consulted,
    # for the reason ``create`` documents.
    if body.tier is not None:
        resolve_tier(body.tier)
    connector_ids: list[int] | None = None
    if body.connectors is not None:
        connector_ids = await connector_ownership.require_owned_connectors(
            db, user_id=current_user.id, ids=body.connectors
        )
    moving = "project_id" in body.model_fields_set
    if moving and body.project_id is not None:
        # Unlocked, for the reason ``create`` gives.
        await project_ownership.require_owned_project(
            db, user_id=current_user.id, project_id=body.project_id
        )

    schedule = await _get_or_404(db, schedule_id, current_user.id, for_update=True)
    activating = lifecycle.apply_owner_edit(
        schedule, recurrence=body.recurrence, status=body.status, now=datetime.now(UTC)
    )
    if activating:
        await _assert_activation_headroom(db, current_user.id, excluding=schedule.id)
    if body.prompt is not None:
        schedule.prompt = body.prompt
    if body.tier is not None:
        schedule.tier = body.tier
    if body.thinking_enabled is not None:
        schedule.thinking_enabled = body.thinking_enabled
    if body.skills is not None:
        schedule.skills = body.skills
    if connector_ids is not None:
        await schedule_connectors.replace(db, schedule.id, connector_ids)
    if moving:
        schedule.project_id = body.project_id
    if body.model_fields_set:
        # Explicit because an edit that only touched the Connectors changes
        # no column the ``onupdate`` would see.
        schedule.updated_at = func.now()
    await db.commit()
    (response,) = await _schedule_responses(db, current_user.id, schedule_id=schedule.id)
    return response


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def delete_schedule(
    request: Request,
    schedule_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> None:
    """Delete a schedule and its run history.

    A hard delete: ``schedule_run`` and ``schedule_mcp_server`` cascade,
    which is the wanted answer — the history and configuration of a schedule
    the owner removed is not something they asked to keep. Sessions the
    schedule already produced are untouched: nothing on ``session``
    references ``schedule``, so they are ordinary sessions from here on.
    """
    schedule = await _get_or_404(db, schedule_id, current_user.id)
    await db.delete(schedule)
    await db.commit()


@router.get("/schedules/runs/unread")
async def count_unread_runs(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> UnreadCountResponse:
    """How many settled occurrences the caller has not looked at."""
    count = await db.scalar(select(func.count()).select_from(_unread(current_user.id).subquery()))
    return UnreadCountResponse(count=count or 0)


@router.post("/schedules/runs/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_runs_read(
    body: MarkRunsReadRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> None:
    """Mark as seen the settled results a list request observed.

    Exactly the ``run_ids`` given, and only those still the caller's and
    still unread — the ids came from a response, but the body is the
    caller's to write.
    """
    if not body.run_ids:
        return
    await db.execute(
        sql_update(ScheduleRun)
        .where(
            ScheduleRun.id.in_(body.run_ids),
            ScheduleRun.user_id == current_user.id,
            ScheduleRun.read_at.is_(None),
        )
        .values(read_at=func.now())
    )
    await db.commit()


@router.get("/schedules/{schedule_id}/runs")
async def list_schedule_runs(
    schedule_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> PaginatedResponse[ScheduleRunResponse]:
    """One schedule's occurrences, newest first."""
    schedule = await _get_or_404(db, schedule_id, current_user.id)
    condition = ScheduleRun.schedule_id == schedule.id
    total = await db.scalar(select(func.count(ScheduleRun.id)).where(condition)) or 0
    rows = await db.execute(
        paginate(
            _runs(condition).order_by(ScheduleRun.created_at.desc(), ScheduleRun.id.desc()),
            page,
            page_size,
        )
    )
    return PaginatedResponse(
        items=[_run_response(row) for row in rows],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )
