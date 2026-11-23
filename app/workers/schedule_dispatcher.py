"""Fire scheduled tasks that have come due.

The dispatcher runs nothing itself. It claims a due ``schedule``, builds a
session, and hands it to ``queue_user_message`` — the same call the message
endpoint makes. The one thing a scheduled turn carries that a typed one does
not is a system reminder telling the model so (:mod:`app.services.schedule.briefing`),
rendered here and stored on the turn as plain text; the worker forwards it
like any other, and still knows nothing about schedules.

Sibling to the settle sweeper and the payment reconciler: same tick
skeleton, its own advisory lock, every host runs it and exactly one wins
each tick.

**Why the enqueue's own commit is allowed to carry our writes.**
``queue_user_message`` commits, and the session row, the ``schedule_run``
history row and the advanced ``next_run_at`` are all staged before it is
called — so one commit makes the whole occurrence atomic, with no window
where a session exists that no history row points at. The rollback half is
just as deliberate:

- a transient failure (database blip) rolls the claim back with everything
  else, so the next tick simply retries the occurrence — it is still inside
  its catch-up grace;
- a terminal refusal (no credits, a misconfigured tier) is caught, and a
  *second* transaction records why and moves the clock on, because retrying
  it would only repeat the refusal every 30 seconds until the grace expired;
- the running-task cap is neither: it is congestion that clears on its own,
  so it is rolled back and retried like a blip, and only written into the
  history once the catch-up window is spent.

The distinction is the whole reason not to commit the claim up front.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import redis.asyncio as aioredis
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.common.text import printable
from app.database import AsyncSessionLocal
from app.errors import (
    ConcurrentTaskLimitError,
    InsufficientCreditsError,
    ModelUnavailableError,
    ProjectNotFoundError,
    UnconfiguredTierError,
)
from app.models.schedule import Schedule, ScheduleRun, ScheduleSkipReason, ScheduleStatus
from app.models.user import User, UserStatus
from app.services.project import ownership as project_ownership
from app.services.schedule import (
    catch_up_grace,
    connectors as schedule_connectors,
    next_run_after,
    render_run_reminder,
)
from app.services.session import build_session, queue_user_message

logger = logging.getLogger(__name__)

# 64-bit key for the Postgres advisory lock. Bytes ``b"SCHEDULE"`` so
# ``pg_locks`` rows are easy to spot, and distinct from the settle
# sweeper's ``b"STLSWEEP"``, the reconciler's ``b"PAYRECON"`` and the
# budget enforcer's ``b"BUDGETNF"``.
_LOCK_KEY = int.from_bytes(b"SCHEDULE", "big")

# Max schedules dispatched per tick. Bounds how long one run can hold the
# advisory lock; the next tick picks up the rest.
_DISPATCH_BATCH = 100

# The unique index whose violation means "this occurrence is already
# recorded". Named so the handler can tell it from every other
# constraint an enqueue might trip.
_OCCURRENCE_INDEX = "uq_schedule_run_occurrence"

# Ceiling on the failure backoff in ``run_forever``. Named because it is
# the longest gap this process can leave between ticks, which is the
# floor under a one-shot's catch-up grace — a grace shorter than this
# would have the dispatcher write off the very occurrences its own
# backoff delayed. ``tests/test_schedule_recurrence.py`` pins that.
_MAX_BACKOFF_SECONDS = 600

# Seconds between ticks. Not a setting: it bounds how late an on-time
# occurrence can be, and it has to stay under the one-shot catch-up grace
# (15 min) or a due one-shot would be picked up past its grace and retired
# without ever running. A knob whose wrong value silently drops the user's
# task is a constant with a reason, not a dial. ``_process_one`` reads it
# directly to judge whether another attempt still fits inside a schedule's
# catch-up window, so ``run_forever``'s ``interval_seconds`` override — which
# nothing currently passes — would have to be threaded through to stay
# truthful.
_TICK_SECONDS = 30

# Consecutive failed occurrences before a schedule auto-pauses. Stops both
# halves of a permanently-broken schedule's damage — the repeated
# notification and the repeated spend — and is reset by any occurrence that
# enqueues. Three is enough to ride out a transient fault and few enough
# that a genuinely dead schedule stops within a few days.
_FAILURES_BEFORE_PAUSE = 3

# How much of the prompt names the session an occurrence creates. Sized to
# the sidebar's own truncation rather than to ``session.title``'s 255-char
# column: a longer title is stored and then cut on screen anyway.
_SESSION_TITLE_CHARS = 40

# Only reachable from a prompt with nothing printable in it, which the schema
# rejects — a belt for rows stored before it did, and to keep the return
# type total.
_UNTITLED_SESSION = "定时任务"

# Refusals that will not resolve by trying again soon: no credits now means
# no credits in thirty seconds, and a misconfigured tier needs an operator.
# Each is recorded as its reason and the clock moves on; what else the
# reason implies is the reason's own business
# (``ScheduleSkipReason.counts_as_failure``). The running-task cap is absent
# on purpose — it is congestion, handled by ``_defer_congestion``.
_REFUSAL_REASONS: dict[type[Exception], ScheduleSkipReason] = {
    InsufficientCreditsError: ScheduleSkipReason.INSUFFICIENT_CREDITS,
    # A tier that does not resolve, or one whose model has no rate-card entry.
    UnconfiguredTierError: ScheduleSkipReason.MISCONFIGURED,
    ModelUnavailableError: ScheduleSkipReason.MISCONFIGURED,
}

# What the ``except`` clause catches and what ``_skip_reason_for`` can classify are
# the same set by construction.
_REFUSALS = tuple(_REFUSAL_REASONS)


async def run_forever(
    redis: aioredis.Redis,
    *,
    interval_seconds: int = _TICK_SECONDS,
) -> None:
    """Periodic dispatch loop. Cancel-safe via :class:`asyncio.CancelledError`."""
    consecutive_failures = 0
    while True:
        try:
            dispatched = await _tick(redis)
            consecutive_failures = 0
            if dispatched:
                logger.info("schedule dispatcher tick: dispatched=%d", dispatched)
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            logger.exception(
                "schedule dispatcher tick failed (consecutive_failures=%d)",
                consecutive_failures,
            )
        await asyncio.sleep(_backoff_delay(interval_seconds, consecutive_failures))


def _backoff_delay(interval_seconds: int, consecutive_failures: int) -> int:
    """How long to wait before the next tick.

    Doubles per consecutive failure up to ``_MAX_BACKOFF_SECONDS``, which
    protects the database when something is fundamentally broken without
    delaying recovery past the point of usefulness.

    The ``max`` on the outside is not decoration: with an interval above the
    ceiling, the inner ``min`` returns *less* than the healthy interval, and
    a failing dispatcher would poll more often than a well one — the exact
    inverse of a backoff. ``_TICK_SECONDS`` sits well below the ceiling so
    that cannot happen today; this keeps the function correct on its own
    terms rather than on one constant's value.
    """
    if not consecutive_failures:
        return interval_seconds
    return max(
        interval_seconds,
        min(interval_seconds * (2**consecutive_failures), _MAX_BACKOFF_SECONDS),
    )


async def _tick(redis: aioredis.Redis) -> int:
    """One dispatch cycle. Returns how many occurrences were acted on
    (0 when another host held the advisory lock).

    Two session lifetimes, same split as the settle sweeper: ``lock_db``
    holds the transaction-scoped advisory lock for the whole tick and only
    reads, while each schedule gets its own :class:`AsyncSession` so a
    database blip on one does not poison the rest of the batch.

    The lock is transaction-scoped (``pg_try_advisory_xact_lock``), so it
    releases when ``lock_db``'s transaction ends however that happens —
    normal exit, crash, SIGKILL, connection loss. There is no manual
    unlock to fail and strand every host's dispatcher.

    Known cost, shared with the settle sweeper: ``lock_db`` sits
    idle-in-transaction for the whole batch, so one schedule whose owner has
    a concurrent send in flight (both take ``FOR UPDATE`` on the same user
    row) delays the rest of the batch and holds back vacuum's xmin horizon
    for that long. The only bound is ``_DISPATCH_BATCH`` times the worst-case
    per-schedule time — this deployment sets neither ``statement_timeout``
    nor ``idle_in_transaction_session_timeout``, so do not read one in.
    Peers skip rather than pile up, which keeps it from compounding across
    hosts. If it ever bites, the fix is to release the lock after the scan:
    correctness does not depend on holding it, since the per-row
    ``FOR UPDATE`` plus the occurrence index already make dispatch
    at-most-once.

    Second known cost, and the price of retrying congestion: a schedule left
    for the next tick keeps its old ``next_run_at``, which is the earliest
    value there is, so it sorts to the front of the candidate ordering and
    stays there for its whole catch-up window. Enough simultaneously
    congested schedules — ``_DISPATCH_BATCH`` of them, which today would take
    twenty owners each at both their active-schedule and running-task caps in
    the same minute — would fill every batch and starve everyone else until
    they aged out. The ``congested`` memo below bounds the wasted work but
    not this; the fix, if it is ever reached, is to page the scan rather than
    take a fixed head.
    """
    async with AsyncSessionLocal() as lock_db:
        got = (
            await lock_db.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _LOCK_KEY}
            )
        ).scalar()
        if not got:
            return 0
        dispatched = 0
        # Owners found at their running-task cap earlier in this batch. Their
        # remaining schedules cannot be enqueued either, and a cap that frees
        # mid-batch is picked up by the next tick anyway now that congestion
        # retries, so re-deriving the answer per schedule only lengthens the
        # tick — and the tick is what holds ``lock_db`` open.
        congested: set[int] = set()
        for schedule_id in await _list_candidates(lock_db):
            if await _process_one(schedule_id, redis, congested=congested):
                dispatched += 1
        return dispatched


async def _list_candidates(db: AsyncSession) -> list[int]:
    """Ids of the schedules due right now, oldest occurrence first.

    Every eligibility condition is in the ``WHERE``, before the ``LIMIT``:
    filtering after it would let a prefix of ineligible rows starve the
    schedules behind them, identically on every tick.

    A schedule whose owner is suspended is simply not selected — it is not
    recorded as skipped either, since the point of suspension is that the
    account stops consuming, and a history nobody can read is not worth
    writing. Re-enabling the account resumes it; the occurrences missed
    meanwhile age out through the ordinary ``too_late`` path.

    Only ids come back. The batch is processed serially, so a late row's
    snapshot may be seconds stale by the time it is reached — the owner
    could have paused or edited it meanwhile. ``_process_one`` re-reads
    under a row lock and decides there.
    """
    rows = await db.execute(
        select(Schedule.id)
        .join(User, User.id == Schedule.user_id)
        .where(
            Schedule.status == ScheduleStatus.ACTIVE,
            Schedule.next_run_at.is_not(None),
            Schedule.next_run_at <= func.now(),
            # The owner's account has to still be live. Every other caller
            # of ``queue_user_message`` sits behind authentication, which is
            # the only other place ``user.status`` is enforced — this one
            # does not, so a suspended account's schedules would otherwise
            # keep creating sessions and spending credits daily, accruing
            # unread badges nobody can log in to see.
            User.status == UserStatus.ACTIVE,
        )
        .order_by(Schedule.next_run_at.asc(), Schedule.id.asc())
        .limit(_DISPATCH_BATCH)
    )
    return list(rows.scalars().all())


async def _process_one(
    schedule_id: int, redis: aioredis.Redis, *, congested: set[int] | None = None
) -> bool:
    """Act on one schedule's due occurrence, in its own transaction(s).

    Returns ``True`` if the occurrence was resolved (enqueued or recorded
    as skipped), ``False`` if it was not actionable, failed, or is being
    left for the next tick to retry. Per-schedule exceptions are caught and
    logged: one bad schedule must not stop the batch.

    ``congested`` is the batch's memo of owners already turned away at their
    running-task cap; this call reads it to skip an enqueue that cannot
    succeed and adds to it when it discovers a new one.
    """
    if congested is None:
        congested = set()
    # Bound before the ``try`` so the handlers can see it: the only way to
    # reach ``IntegrityError`` is through ``_enqueue``, which runs after this
    # is set, but a handler reading a name the checker cannot prove is bound
    # is a trap for the next edit rather than a fact about this one.
    occurrence: datetime | None = None
    try:
        async with AsyncSessionLocal() as db:
            schedule = await _lock(db, schedule_id)
            occurrence = _due_occurrence(schedule)
            if schedule is None or occurrence is None:
                return False

            if not _zone_is_resolvable(schedule.timezone):
                await _pause_unresolvable(db, schedule, occurrence)
                return True

            deadline = occurrence + catch_up_grace(schedule.frequency)
            if datetime.now(UTC) > deadline:
                # Too far behind to be worth delivering. Record it so the
                # gap is explained in the history, and fast-forward rather
                # than replaying every occurrence the outage covered.
                _settle(db, schedule, occurrence, skip=ScheduleSkipReason.TOO_LATE)
                await db.commit()
                return True

            if schedule.user_id in congested:
                # Same answer as the attempt below, without paying for it.
                return await _defer_congestion(db, schedule_id, occurrence, deadline)

            try:
                await _enqueue(db, redis, schedule, occurrence)
            except ConcurrentTaskLimitError:
                congested.add(schedule.user_id)
                return await _defer_congestion(db, schedule_id, occurrence, deadline)
            except _REFUSALS as exc:
                await db.rollback()
                await _settle_skip(db, schedule_id, occurrence, _skip_reason_for(exc))
            return True
    except IntegrityError as exc:
        if _constraint_of(exc) != _OCCURRENCE_INDEX:
            # Some *other* constraint failed inside the enqueue — a credit
            # ledger idempotency key, a file binding. Treating it as "already
            # recorded" would advance the clock, write no history row at all,
            # and point whoever reads the log at the wrong cause. Let it fall
            # to the generic arm, which leaves the occurrence to be retried
            # and eventually written off honestly.
            logger.exception("schedule dispatcher: schedule %s failed", schedule_id)
            return False
        # The occurrence index fired: this occurrence is already recorded.
        # The row lock above normally makes that unreachable, so this is the
        # backstop doing its job — a rejected insert instead of a second run
        # of the user's task. Move the clock on, or every tick would retry
        # the same collision.
        logger.warning(
            "schedule dispatcher: occurrence already recorded for schedule %s; advancing",
            schedule_id,
        )
        try:
            # Non-None by construction: the collision comes from ``_enqueue``,
            # which is only reached once the occurrence is settled.
            assert occurrence is not None
            await _advance_only(schedule_id, occurrence)
        except Exception:
            # The sibling ``except Exception`` cannot catch what is raised
            # inside this handler, and letting it out would abandon every
            # remaining schedule in the batch — the one thing
            # ``_process_one`` promises not to do.
            logger.exception("schedule dispatcher: advancing schedule %s failed", schedule_id)
        return False
    except Exception:
        logger.exception("schedule dispatcher: schedule %s failed", schedule_id)
        return False


def _session_title(prompt: str) -> str:
    """A name for the session an occurrence creates.

    A schedule has no title of its own — its prompt is what identifies it —
    but the session lands in the user's sidebar beside typed ones, and an
    untitled row there is a row they cannot pick out.

    The first line, trimmed. Deliberately not the LLM summariser a typed
    session gets (``services/session/title.generate_title``): that is an
    extra paid call per occurrence, on a schedule that may fire daily for
    months, to name something whose text the user wrote themselves and will
    recognise.

    A long prompt is cut at the limit, and aligned back to a word boundary
    **only when one falls in the second half** of what is kept. That
    condition is the rule, not a rounding: honouring any space at all would
    let a prompt whose first space sits at character three produce a
    three-character title, and CJK text — which has no word spaces — would
    lose most of its title to whatever space happened to appear. Where no
    late space exists the cut lands mid-word, which is a legible cost for a
    title that still carries the full forty characters.
    """
    lines = printable(prompt).strip().splitlines()
    first_line = lines[0].strip() if lines else ""
    if not first_line:
        return _UNTITLED_SESSION
    if len(first_line) <= _SESSION_TITLE_CHARS:
        return first_line
    cut = first_line[:_SESSION_TITLE_CHARS]
    # Only a space in the second half counts — see the docstring for why an
    # early one is worse than a mid-word cut.
    spaced = cut.rsplit(" ", 1)[0]
    if len(spaced) >= _SESSION_TITLE_CHARS // 2:
        cut = spaced
    return cut.rstrip() + "…"


def _zone_is_resolvable(timezone: str) -> bool:
    """Whether this process can build the schedule's zone.

    The API validated the name against *its* tz database; this process may
    not have the same one (a slimmer image, a different tzdata). Every path
    out of here runs the recurrence, which needs the zone — so an
    unresolvable one is checked once, up front, rather than raising from
    inside whichever branch happened to be taken.
    """
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


async def _pause_unresolvable(db: AsyncSession, schedule: Schedule, occurrence: datetime) -> None:
    """Stop a schedule whose zone this process cannot build, and say why.

    The clock cannot move: advancing means computing the next occurrence,
    which is the thing that cannot be done. Pausing is what makes this
    terminal instead of an every-tick traceback that changes nothing.

    The occurrence is recorded ``misconfigured`` — a deployment fault the
    owner has to notice, like a tier that no longer resolves — so the history
    explains the gap. It cannot collide with a later run: resuming re-arms
    the schedule from *now* (``lifecycle.arm``), strictly after this one.
    """
    logger.error(
        "schedule dispatcher: schedule %s has an unresolvable timezone %r; pausing",
        schedule.id,
        schedule.timezone,
    )
    _record(db, schedule, occurrence, skip=ScheduleSkipReason.MISCONFIGURED)
    schedule.status = ScheduleStatus.PAUSED
    await db.commit()


def _constraint_of(exc: IntegrityError) -> str | None:
    """Which database constraint this violation names, if it says.

    ``psycopg`` carries it on the driver exception's diagnostics. Reading
    the name rather than matching the message keeps the check working
    across locales and driver versions; ``None`` (a driver that does not
    report one) is treated as "not ours", which fails toward the honest
    generic path.
    """
    diagnostics = getattr(exc.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


def _skip_reason_for(exc: Exception) -> ScheduleSkipReason:
    """The skip reason a caught refusal is settled as.

    Matched with ``isinstance``, the way ``except _REFUSALS`` caught it, so a
    more specific subclass of a listed refusal still classifies.
    """
    for refusal, reason in _REFUSAL_REASONS.items():
        if isinstance(exc, refusal):
            return reason
    raise AssertionError(f"unclassified refusal: {type(exc).__name__}")


async def _lock(db: AsyncSession, schedule_id: int) -> Schedule | None:
    """Re-read the schedule under ``FOR UPDATE``."""
    return (
        await db.execute(select(Schedule).where(Schedule.id == schedule_id).with_for_update())
    ).scalar_one_or_none()


async def _relock_for(db: AsyncSession, schedule_id: int, occurrence: datetime) -> Schedule | None:
    """Re-lock a schedule that is *still* waiting on ``occurrence``.

    Both second-transaction paths reach this after their first transaction
    released the row lock, and in that window the row can have moved on
    without them: the owner can pause it, delete it, or **edit the
    recurrence**, which recomputes ``next_run_at``. A peer worker can also
    have taken the same occurrence and advanced past it.

    Anything but "still active, still stopped on this occurrence" means
    somebody else owns what happens next. Writing anyway would either
    overwrite the clock they just set — silently swallowing the run the
    owner had just scheduled — or advance a second time from an occurrence
    that was already accounted for, losing a delivery with no trace in the
    history.
    """
    schedule = await _lock(db, schedule_id)
    if schedule is None or schedule.status is not ScheduleStatus.ACTIVE:
        return None
    if schedule.next_run_at != occurrence:
        logger.info(
            "schedule dispatcher: schedule %s moved on from occurrence %s; leaving it alone",
            schedule_id,
            occurrence.isoformat(),
        )
        return None
    return schedule


def _due_occurrence(schedule: Schedule | None) -> datetime | None:
    """The occurrence to act on, or ``None`` if this row is no longer due.

    Re-checked under the lock because the candidate snapshot is stale: the
    owner may have paused the schedule, edited its time, or a peer may have
    taken the occurrence between the scan and the lock.
    """
    if schedule is None or schedule.status is not ScheduleStatus.ACTIVE:
        return None
    if schedule.next_run_at is None or schedule.next_run_at > datetime.now(UTC):
        return None
    return schedule.next_run_at


async def _enqueue(
    db: AsyncSession,
    redis: aioredis.Redis,
    schedule: Schedule,
    occurrence: datetime,
) -> None:
    """Stand up the session and enqueue the turn.

    Nothing here commits. ``queue_user_message`` does, and it carries the
    session, the history row and the advanced clock with it — see the
    module docstring for why that coupling is the point rather than a
    shortcut.
    """
    session = await build_session(
        db,
        user_id=schedule.user_id,
        title=_session_title(schedule.prompt),
        tier=schedule.tier,
        agent_type=schedule.agent_type,
        thinking_enabled=schedule.thinking_enabled,
        # Copied onto the new session, which from here follows the ordinary
        # path: its enqueue re-reads each Connector by id and keeps only the
        # ones still owned and still connected. So a Connector the owner
        # disconnected between two firings simply stops appearing.
        #
        # A Connector that fails to *connect* is a quieter story, and worth
        # knowing about: the worker skips it and tells the model so
        # (``agent-worker/runtime/connectors.py``), but the turn succeeds.
        # An unattended run therefore produces a worse answer rather than an
        # error, and ``schedule_run`` cannot see the difference.
        connector_ids=await schedule_connectors.configured_ids(db, schedule.id),
        project_id=await _project_to_run_in(db, schedule),
    )
    _settle(db, schedule, occurrence, session_id=session.id)
    await queue_user_message(
        db,
        redis,
        session,
        content=schedule.prompt,
        files=[],
        # Names, resolved fresh every firing. Since the worker's listing
        # narrowed to live SYSTEM skills plus the turn's own picks, this
        # argument is the only thing that puts an OFFICIAL or CUSTOM skill
        # in a scheduled run's reach; without it the model sees none.
        # Whatever no longer resolves is dropped here rather than failing
        # the occurrence — a schedule outlives the skills it names.
        skills=schedule.skills,
        # Nobody is there to answer a clarifying question, and "today" means
        # the owner's zone at the occurrence, not the host clock at enqueue.
        reminders=[render_run_reminder(schedule, occurrence)],
    )


async def _project_to_run_in(db: AsyncSession, schedule: Schedule) -> uuid.UUID | None:
    """The project the occurrence's session is created in: the schedule's,
    while it is live, and none once it is deleted (see ``Schedule.project_id``).

    Locked, as every write that creates a session in a project locks it
    first, so the project cannot be deleted before the session lands in it.
    The schedule is already locked, and no path locks a project and then a
    schedule. ``updated_at`` is left alone: it tracks what the owner did to
    the project, and a firing is not that.
    """
    if schedule.project_id is None:
        return None
    try:
        project = await project_ownership.require_owned_project(
            db, user_id=schedule.user_id, project_id=schedule.project_id, for_update=True
        )
    except ProjectNotFoundError:
        return None
    return project.id


async def _defer_congestion(
    db: AsyncSession, schedule_id: int, occurrence: datetime, deadline: datetime
) -> bool:
    """Hold a congested occurrence for the next tick, or settle it if none fits.

    Being at the running-task cap is not a refusal: a slot frees the moment
    one of the owner's other tasks finishes. So the attempt is rolled back and
    ``next_run_at`` left alone — the same recovery a transient database error
    gets, and what the catch-up window exists for. Writing the occurrence off
    instead would lose a whole report to a thirty-second overlap.

    Only once no further attempt fits is it recorded, while its cause is still
    known. The margin is one healthy tick, deliberately optimistic: the real
    gap also carries tick duration, batch position and any backoff, so the
    last attempt can land past ``deadline`` and be recorded ``too_late``
    instead. A generous margin would be the worse error — it would give up on
    attempts that could still have delivered.

    Returns ``True`` if the occurrence was settled, ``False`` if deferred.
    """
    await db.rollback()
    if datetime.now(UTC) + timedelta(seconds=_TICK_SECONDS) <= deadline:
        return False
    await _settle_skip(db, schedule_id, occurrence, ScheduleSkipReason.CONCURRENCY_LIMIT)
    return True


async def _settle_skip(
    db: AsyncSession,
    schedule_id: int,
    occurrence: datetime,
    reason: ScheduleSkipReason,
) -> None:
    """Settle an occurrence as skipped, in a fresh transaction.

    Re-locks rather than reusing the row the failed attempt held: the
    rollback discarded what that attempt staged, including the failure-count
    reset a delivery would have made, and only a fresh read sees the committed
    count the automatic pause depends on. If the row vanished, was paused or
    moved on meanwhile, the owner already decided and there is nothing to
    record.
    """
    schedule = await _relock_for(db, schedule_id, occurrence)
    if schedule is None:
        return
    logger.info(
        "schedule dispatcher: schedule %s occurrence %s skipped (%s)",
        schedule_id,
        occurrence.isoformat(),
        reason.value,
    )
    _settle(db, schedule, occurrence, skip=reason)
    await db.commit()


async def _advance_only(schedule_id: int, occurrence: datetime) -> None:
    """Move a schedule's clock on without recording anything.

    ``occurrence`` is the one that collided, and passing it rather than
    re-reading ``next_run_at`` is the whole point: by the time this runs the
    winning worker has already advanced the row, so the stored value is the
    *next* occurrence. Anchoring on that would skip it — a delivery lost
    with no ``schedule_run`` row to explain it, and for a one-shot a
    ``COMPLETED`` that never ran.
    """
    async with AsyncSessionLocal() as db:
        schedule = await _relock_for(db, schedule_id, occurrence)
        if schedule is None:
            return
        # Another worker delivered it; for a one-shot that is the end.
        _advance(schedule, occurrence, retire_one_shot=True)
        await db.commit()


def _settle(
    db: AsyncSession,
    schedule: Schedule,
    occurrence: datetime,
    *,
    session_id: uuid.UUID | None = None,
    skip: ScheduleSkipReason | None = None,
) -> None:
    """Stage an occurrence's outcome: its history row, and what that outcome
    does to the schedule.

    The one place an outcome is applied, so every path that ends an
    occurrence — delivered, refused, or too late — has the same consequences:
    a delivery resets the failure streak; a skip that counts as a failure
    extends it and pauses the schedule at the threshold; and the clock moves
    to the next occurrence, or a one-shot is retired or paused according to
    the reason. Exactly one of ``session_id`` / ``skip`` is set, and
    ``ck_schedule_run_outcome`` rejects a row claiming both.
    """
    _record(db, schedule, occurrence, session_id=session_id, skip=skip)
    if skip is None:
        schedule.consecutive_failures = 0
    elif skip.counts_as_failure:
        schedule.consecutive_failures += 1
        if schedule.consecutive_failures >= _FAILURES_BEFORE_PAUSE:
            # Stops both the repeated notification and the repeated spend.
            schedule.status = ScheduleStatus.PAUSED
            logger.warning(
                "schedule dispatcher: schedule %s paused after %d consecutive failures",
                schedule.id,
                schedule.consecutive_failures,
            )
    _advance(schedule, occurrence, retire_one_shot=skip is None or skip.retires_one_shot)


def _record(
    db: AsyncSession,
    schedule: Schedule,
    occurrence: datetime,
    *,
    session_id: uuid.UUID | None = None,
    skip: ScheduleSkipReason | None = None,
) -> None:
    """Stage the history row for one occurrence."""
    db.add(
        ScheduleRun(
            schedule_id=schedule.id,
            user_id=schedule.user_id,
            scheduled_for=occurrence,
            session_id=session_id,
            skip_reason=skip,
        )
    )


def _advance(schedule: Schedule, occurrence: datetime, *, retire_one_shot: bool) -> None:
    """Point the schedule at its next occurrence, or end a one-shot.

    The anchor is ``max(occurrence, now - grace)``. No earlier than the
    occurrence just handled, so it is never handled twice; no earlier than the
    start of the catch-up window, so an outage writes off only the occurrences
    that are genuinely stale — fast-forwarding from ``now()`` would also skip
    the most recent one, even when it is minutes late and still deliverable.
    The result lands exactly on ``at_time`` in the schedule's zone, because
    the grid is recomputed from that column rather than stepped from the last
    run.

    ``retire_one_shot`` decides a one-shot with no next occurrence —
    ``COMPLETED`` when its moment is spent, ``PAUSED`` with its instant kept
    when the cause may clear and the owner can re-arm it.
    """
    anchor = max(occurrence, datetime.now(UTC) - catch_up_grace(schedule.frequency))
    next_run = next_run_after(schedule, anchor)
    if next_run is not None:
        schedule.next_run_at = next_run
    elif retire_one_shot:
        schedule.next_run_at = None
        schedule.status = ScheduleStatus.COMPLETED
    else:
        schedule.status = ScheduleStatus.PAUSED
