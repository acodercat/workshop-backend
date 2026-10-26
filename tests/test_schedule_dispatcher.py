"""Scheduled-task dispatcher: candidate selection and outcome routing.

Two halves, tested apart. The candidate query is asserted as compiled SQL
(no database); the routing is asserted with the DB session and the two
collaborators it calls — ``build_session`` and ``queue_user_message`` —
stubbed, so what is under test is which outcome each refusal produces.
"""

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.errors import (
    ConcurrentTaskLimitError,
    FileNotOwnedError,
    FileNotUploadedError,
    FileTooLargeError,
    InsufficientCreditsError,
    UnconfiguredTierError,
)
from app.models.schedule import (
    Schedule,
    ScheduleFrequency,
    ScheduleSkipReason,
    ScheduleStatus,
)
from app.models.session import AgentType, Tier
from app.services.schedule import catch_up_grace, render_run_reminder
from app.workers import schedule_dispatcher


class _Result:
    def scalars(self):
        return self

    def all(self) -> list:
        return []


class _CapturingDb:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result()


async def test_list_candidates_filters_before_limit() -> None:
    # Filtering after LIMIT would let a prefix of ineligible rows starve
    # the schedules behind them, identically on every tick.
    db = _CapturingDb()

    await schedule_dispatcher._list_candidates(db)

    assert db.statement is not None
    sql = str(
        db.statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "schedule.status = 'ACTIVE'" in sql
    assert "schedule.next_run_at IS NOT NULL" in sql
    assert "schedule.next_run_at <= now()" in sql
    # The owner's account has to still be live: this is the only
    # ``queue_user_message`` caller not behind authentication, which is
    # where ``user.status`` is otherwise enforced. Without it a suspended
    # account's schedules keep creating sessions and spending credits.
    assert "\"user\".status = 'ACTIVE'" in sql
    assert sql.index("WHERE") < sql.index("LIMIT")


# ── outcome routing ──────────────────────────────────────────────────────


# Columns the dispatcher mutates on a schedule. A real rollback expires
# these and the re-SELECT in ``_settle_skip`` reads the committed values
# back; without modelling that, a fake would let the failed attempt's
# in-memory writes leak into the refusal path and hide a real ordering bug.
_MUTATED = ("status", "next_run_at", "consecutive_failures")


class _FakeDb:
    def __init__(self, schedule: Schedule) -> None:
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0
        self._schedule = schedule
        self._committed = {name: getattr(schedule, name) for name in _MUTATED}
        self._durable = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1
        self._committed = {name: getattr(self._schedule, name) for name in _MUTATED}
        self._durable = len(self.added)

    async def rollback(self) -> None:
        self.rollbacks += 1
        for name, value in self._committed.items():
            setattr(self._schedule, name, value)
        # Pending rows go with the transaction, so a failed attempt leaves
        # no history row behind.
        del self.added[self._durable :]


class _FakeSession:
    """Stands in for ``AsyncSessionLocal()``.

    Closing on an exception rolls back, which is what SQLAlchemy does and
    what the dispatcher relies on: an enqueue that raises past the refusal
    arms must leave the clock where it was, so the next tick retries the
    occurrence rather than skipping it.
    """

    def __init__(self, db) -> None:
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, *_rest) -> bool:
        if exc_type is not None:
            await self._db.rollback()
        return False


def _schedule(*, late_by: timedelta = timedelta(minutes=1), **overrides) -> Schedule:
    spec = {
        "id": 7,
        "user_id": 42,
        "prompt": "Summarise today's news",
        "status": ScheduleStatus.ACTIVE,
        "frequency": ScheduleFrequency.DAILY,
        "at_time": time(9, 0),
        "timezone": "Asia/Shanghai",
        "run_date": None,
        "weekday": None,
        "day_of_month": None,
        "next_run_at": datetime.now(UTC) - late_by,
        "consecutive_failures": 0,
        "skills": [],
        "tier": Tier.STANDARD,
        "agent_type": AgentType.FULL,
        "thinking_enabled": True,
    }
    return Schedule(**{**spec, **overrides})


def _patch(
    monkeypatch,
    schedule,
    *,
    queue_raises: Exception | list[Exception | None] | None = None,
    connectors: tuple[int, ...] = (),
    attachments: tuple = (),
) -> tuple:
    db = _FakeDb(schedule)
    calls: dict[str, list] = {"build": [], "queue": []}

    monkeypatch.setattr(schedule_dispatcher, "AsyncSessionLocal", lambda: _FakeSession(db))

    async def fake_lock(_db, _schedule_id):
        return schedule

    async def fake_build(_db, **kwargs):
        calls["build"].append(kwargs)
        return SimpleNamespace(id=uuid4())

    # A list is consumed one entry per attempt, which is how a retry that
    # later succeeds is expressed; anything else applies to every attempt.
    scripted = queue_raises if isinstance(queue_raises, list) else None

    async def fake_queue(_db, _redis, _session, **kwargs):
        calls["queue"].append(kwargs)
        if scripted is not None:
            exc = scripted.pop(0) if scripted else None
        else:
            exc = queue_raises
        if exc is not None:
            raise exc

    async def fake_configured_ids(_db, _schedule_id):
        return list(connectors)

    async def fake_attached(_db, _schedule_id):
        return list(attachments)

    monkeypatch.setattr(
        schedule_dispatcher.schedule_connectors, "configured_ids", fake_configured_ids
    )
    monkeypatch.setattr(schedule_dispatcher.schedule_files, "attached", fake_attached)
    monkeypatch.setattr(schedule_dispatcher, "_lock", fake_lock)
    monkeypatch.setattr(schedule_dispatcher, "build_session", fake_build)
    monkeypatch.setattr(schedule_dispatcher, "queue_user_message", fake_queue)
    return db, calls


def _runs(db: _FakeDb) -> list:
    return [obj for obj in db.added if obj.__class__.__name__ == "ScheduleRun"]


class TestCarriedContext:
    """What a firing hands the session besides the prompt.

    Both are the only route their kind has into a scheduled run: the
    worker's skill listing narrowed to live SYSTEM skills plus the turn's
    own picks, and a session with no ``session_mcp_server`` rows connects
    nothing at all — silently, without even a notice to the model.
    """

    async def test_passes_the_schedules_skills_to_the_enqueue(self, monkeypatch):
        schedule = _schedule(skills=["pptx", "web-research"])
        _, calls = _patch(monkeypatch, schedule)

        await schedule_dispatcher._process_one(7, redis=None)

        # Names, in the owner's order — the worker lists them to the model
        # that way. Resolution happens inside ``queue_user_message``.
        assert calls["queue"][0]["skills"] == ["pptx", "web-research"]

    async def test_briefs_the_model_on_the_occurrence_it_is_running(self, monkeypatch):
        schedule = _schedule(late_by=timedelta(minutes=40))
        occurrence = schedule.next_run_at
        _, calls = _patch(monkeypatch, schedule)

        await schedule_dispatcher._process_one(7, redis=None)

        # Rendered for the occurrence that came due, not the moment of the
        # enqueue — this one is forty minutes late.
        assert calls["queue"][0]["reminders"] == [render_run_reminder(schedule, occurrence)]

    async def test_passes_the_schedules_connectors_to_the_session(self, monkeypatch):
        schedule = _schedule()
        _, calls = _patch(monkeypatch, schedule, connectors=(3, 1))

        await schedule_dispatcher._process_one(7, redis=None)

        assert calls["build"][0]["connector_ids"] == [3, 1]

    async def test_passes_the_schedules_attachments_to_the_enqueue(self, monkeypatch):
        schedule = _schedule()
        _, calls = _patch(
            monkeypatch,
            schedule,
            attachments=(
                SimpleNamespace(
                    file_key="session-files/abc/brief.pdf",
                    filename="brief.pdf",
                    mime_type="application/pdf",
                    size_bytes=2048,
                ),
            ),
        )

        await schedule_dispatcher._process_one(7, redis=None)

        # ``size_bytes`` comes off the stored row, which holds what OSS
        # confirmed — not a number any client ever supplied.
        (file_ref,) = calls["queue"][0]["files"]
        assert file_ref.file_key == "session-files/abc/brief.pdf"
        assert file_ref.size_bytes == 2048

    async def test_the_same_object_is_rebound_on_every_firing(self, monkeypatch):
        # What ``uq_session_files_session_id_file_key`` exists to allow: two
        # firings, two sessions, one file.
        schedule = _schedule()
        attachment = SimpleNamespace(
            file_key="session-files/abc/brief.pdf",
            filename="brief.pdf",
            mime_type=None,
            size_bytes=1,
        )
        _, calls = _patch(monkeypatch, schedule, attachments=(attachment,))

        await schedule_dispatcher._process_one(7, redis=None)
        schedule.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
        await schedule_dispatcher._process_one(7, redis=None)

        assert [f[0].file_key for f in (c["files"] for c in calls["queue"])] == [
            "session-files/abc/brief.pdf",
            "session-files/abc/brief.pdf",
        ]

    async def test_carries_nothing_when_nothing_was_configured(self, monkeypatch):
        # The default, and the shape every schedule created before this
        # feature still has.
        schedule = _schedule()
        _, calls = _patch(monkeypatch, schedule)

        await schedule_dispatcher._process_one(7, redis=None)

        assert calls["queue"][0]["skills"] == []
        assert calls["build"][0]["connector_ids"] == []
        assert calls["queue"][0]["files"] == []


class TestHappyPath:
    async def test_enqueues_and_records_the_occurrence(self, monkeypatch):
        schedule = _schedule()
        occurrence = schedule.next_run_at
        db, calls = _patch(monkeypatch, schedule)

        assert await schedule_dispatcher._process_one(7, redis=None) is True

        assert calls["build"][0]["user_id"] == 42
        assert calls["queue"][0]["content"] == "Summarise today's news"
        run = _runs(db)[0]
        assert run.scheduled_for == occurrence
        assert run.session_id is not None
        assert run.skip_reason is None

    async def test_does_not_commit_itself(self, monkeypatch):
        # The enqueue's own commit carries the session, the history row and
        # the advanced clock together. Committing here first would leave a
        # claim standing that a failed enqueue could no longer roll back.
        db, _ = _patch(monkeypatch, _schedule())

        await schedule_dispatcher._process_one(7, redis=None)

        assert db.commits == 0

    async def test_advances_the_clock_and_clears_the_failure_count(self, monkeypatch):
        schedule = _schedule(consecutive_failures=2)
        occurrence = schedule.next_run_at
        _patch(monkeypatch, schedule)

        await schedule_dispatcher._process_one(7, redis=None)

        # Past the occurrence just settled, not past *now*: the next grid
        # point can legitimately be minutes ago when catching up.
        assert schedule.next_run_at > occurrence
        assert schedule.consecutive_failures == 0


class TestNotActionable:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"status": ScheduleStatus.PAUSED},
            {"next_run_at": datetime.now(UTC) + timedelta(hours=1)},
            {"next_run_at": None},
        ],
        ids=["paused-since-the-scan", "not-due-yet", "no-next-run"],
    )
    async def test_leaves_the_row_alone(self, monkeypatch, overrides):
        # The candidate list is a stale snapshot; the owner may have paused
        # or edited the schedule between the scan and the lock.
        schedule = _schedule(**overrides)
        db, calls = _patch(monkeypatch, schedule)

        assert await schedule_dispatcher._process_one(7, redis=None) is False
        assert calls["queue"] == []
        assert _runs(db) == []


class TestTooLate:
    async def test_records_a_skip_and_fast_forwards(self, monkeypatch):
        # Three hours past a two-hour grace: not worth delivering, and the
        # occurrences an outage covered are not replayed one per tick.
        schedule = _schedule(late_by=timedelta(hours=3))
        db, calls = _patch(monkeypatch, schedule)

        assert await schedule_dispatcher._process_one(7, redis=None) is True

        assert calls["queue"] == []
        assert _runs(db)[0].skip_reason is ScheduleSkipReason.TOO_LATE
        # The clock never lands on something already too late to deliver —
        # that is the whole invariant, not "lands in the future".
        assert schedule.next_run_at >= datetime.now(UTC) - catch_up_grace(schedule.frequency)
        assert db.commits == 1

    async def test_it_does_not_skip_a_still_deliverable_occurrence(self, monkeypatch):
        # An outage should cost the deliveries that are genuinely stale and
        # no more. Fast-forwarding from now() would jump clean over the most
        # recent occurrence even when it is minutes old: a 09:00 schedule
        # whose process came back at 09:30 on day four would write off day
        # one and then land on day *five*, losing a report half an hour late.
        shanghai = ZoneInfo("Asia/Shanghai")
        recent = datetime.now(shanghai) - timedelta(minutes=30)
        schedule = _schedule(
            at_time=recent.time().replace(second=0, microsecond=0),
            next_run_at=datetime.now(UTC) - timedelta(days=3),
        )
        _patch(monkeypatch, schedule)

        await schedule_dispatcher._process_one(7, redis=None)

        # Lands on today's occurrence — half an hour old, well inside the
        # two-hour grace — so the very next tick delivers it.
        assert schedule.next_run_at < datetime.now(UTC)
        assert datetime.now(UTC) - schedule.next_run_at < catch_up_grace(schedule.frequency)

    async def test_does_not_count_as_a_failure(self, monkeypatch):
        # An outage is the platform's fault; pausing the user's schedule
        # for it would compound the outage.
        schedule = _schedule(late_by=timedelta(hours=3))
        _patch(monkeypatch, schedule)

        await schedule_dispatcher._process_one(7, redis=None)

        assert schedule.consecutive_failures == 0


class TestRefusals:
    @pytest.mark.parametrize(
        ("exc", "reason"),
        [
            (InsufficientCreditsError("no credits"), ScheduleSkipReason.INSUFFICIENT_CREDITS),
            (UnconfiguredTierError("bad tier"), ScheduleSkipReason.MISCONFIGURED),
            (FileNotUploadedError("gone"), ScheduleSkipReason.ATTACHMENT_UNAVAILABLE),
            (FileTooLargeError(9, 5), ScheduleSkipReason.ATTACHMENT_UNAVAILABLE),
            (FileNotOwnedError("not yours"), ScheduleSkipReason.ATTACHMENT_UNAVAILABLE),
        ],
        ids=[
            "no-credits",
            "misconfigured-tier",
            "attachment-deleted",
            "attachment-too-large",
            "attachment-not-owned",
        ],
    )
    async def test_records_the_reason_and_moves_on(self, monkeypatch, exc, reason):
        schedule = _schedule()
        occurrence = schedule.next_run_at
        db, _ = _patch(monkeypatch, schedule, queue_raises=exc)

        assert await schedule_dispatcher._process_one(7, redis=None) is True

        # The half-built session and turn are discarded, then a second
        # transaction records why. Retrying instead would repeat the same
        # refusal every tick until the grace ran out.
        assert db.rollbacks == 1
        assert db.commits == 1
        assert _runs(db)[-1].skip_reason is reason
        # Past the occurrence just settled, not past *now*: the next grid
        # point can legitimately be minutes ago when catching up.
        assert schedule.next_run_at > occurrence
        # Which reasons count is the reason's own rule (pinned in
        # ``test_schedule_model``); the dispatcher only has to honour it.
        assert schedule.consecutive_failures == (1 if reason.counts_as_failure else 0)

    async def test_the_concurrency_cap_never_pauses_a_schedule(self, monkeypatch):
        # Congestion means the owner has other work running right now.
        # Counting it would pause a good schedule for being popular. Timed to
        # the end of the window so it settles rather than retries — the
        # settling path is the only one that could increment the counter.
        schedule = _schedule(
            consecutive_failures=schedule_dispatcher._FAILURES_BEFORE_PAUSE - 1,
            late_by=catch_up_grace(ScheduleFrequency.DAILY) - timedelta(seconds=5),
        )
        _patch(monkeypatch, schedule, queue_raises=ConcurrentTaskLimitError(10))

        await schedule_dispatcher._process_one(7, redis=None)

        assert schedule.status is ScheduleStatus.ACTIVE

    async def test_pauses_once_failures_reach_the_threshold(self, monkeypatch):
        # Stops both halves of a broken schedule's damage: the repeated
        # notification and the repeated spend.
        schedule = _schedule(consecutive_failures=schedule_dispatcher._FAILURES_BEFORE_PAUSE - 1)
        _patch(monkeypatch, schedule, queue_raises=InsufficientCreditsError("no credits"))

        await schedule_dispatcher._process_one(7, redis=None)

        assert schedule.status is ScheduleStatus.PAUSED


class TestCongestion:
    """The running-task cap is transient, so the occurrence is not spent on it.

    Everything else in ``_REFUSAL_REASONS`` is terminal for the occurrence. This
    one clears on its own the moment one of the owner's other tasks finishes,
    which is usually long before the catch-up window closes.
    """

    async def test_retries_rather_than_writing_the_occurrence_off(self, monkeypatch):
        # The failure mode this guards: a daily report lost for the whole day
        # to a thirty-second overlap with the owner's own work.
        schedule = _schedule()
        due = schedule.next_run_at
        db, _ = _patch(monkeypatch, schedule, queue_raises=ConcurrentTaskLimitError(10))

        assert await schedule_dispatcher._process_one(7, redis=None) is False

        # Nothing was recorded and the clock did not move, so the next tick
        # arrives at exactly the same occurrence and tries again.
        assert _runs(db) == []
        assert db.commits == 0
        assert db.rollbacks == 1
        assert schedule.next_run_at == due
        assert schedule.status is ScheduleStatus.ACTIVE
        assert schedule.consecutive_failures == 0

    async def test_keeps_retrying_for_as_long_as_the_window_allows(self, monkeypatch):
        # One tick short of the deadline is still a retry: the attempt fits.
        grace = catch_up_grace(ScheduleFrequency.DAILY)
        tick = timedelta(seconds=schedule_dispatcher._TICK_SECONDS)
        schedule = _schedule(late_by=grace - tick - timedelta(seconds=5))
        db, _ = _patch(monkeypatch, schedule, queue_raises=ConcurrentTaskLimitError(10))

        assert await schedule_dispatcher._process_one(7, redis=None) is False
        assert _runs(db) == []

    async def test_delivers_once_the_cap_frees(self, monkeypatch):
        # The whole point of retrying, and the case no other test covers:
        # the occurrence survives the congestion and is delivered later.
        schedule = _schedule()
        occurrence = schedule.next_run_at
        db, calls = _patch(
            monkeypatch,
            schedule,
            queue_raises=[ConcurrentTaskLimitError(10), None],
        )

        assert await schedule_dispatcher._process_one(7, redis=None) is False
        assert await schedule_dispatcher._process_one(7, redis=None) is True

        # One occurrence, one history row, one advance: retrying neither ran
        # the owner's task twice nor double-stepped the clock.
        assert len(calls["queue"]) == 2
        (run,) = _runs(db)
        assert run.scheduled_for == occurrence
        assert run.session_id is not None
        assert run.skip_reason is None
        assert schedule.next_run_at > occurrence

    async def test_one_owner_is_only_probed_once_per_batch(self, monkeypatch):
        # Their other schedules cannot be enqueued either, and the tick is
        # what holds the advisory lock open.
        schedule = _schedule()
        db, calls = _patch(monkeypatch, schedule, queue_raises=ConcurrentTaskLimitError(10))
        congested: set[int] = set()

        assert await schedule_dispatcher._process_one(7, redis=None, congested=congested) is False
        assert congested == {42}
        assert await schedule_dispatcher._process_one(8, redis=None, congested=congested) is False

        assert len(calls["queue"]) == 1

    async def test_the_shortcut_still_settles_at_the_deadline(self, monkeypatch):
        # Skipping the attempt must not swallow the last chance to say why.
        schedule = _schedule(late_by=catch_up_grace(ScheduleFrequency.DAILY) - timedelta(seconds=5))
        db, calls = _patch(monkeypatch, schedule)

        assert await schedule_dispatcher._process_one(7, redis=None, congested={42}) is True

        assert calls["queue"] == []
        assert _runs(db)[-1].skip_reason is ScheduleSkipReason.CONCURRENCY_LIMIT

    async def test_settles_with_the_specific_reason_on_the_last_attempt(self, monkeypatch):
        # Once no further attempt fits, it is recorded while the cause is
        # still known. Letting it fall through to the next tick would write
        # ``too_late``, which tells the owner nothing they can act on.
        schedule = _schedule(late_by=catch_up_grace(ScheduleFrequency.DAILY) - timedelta(seconds=5))
        occurrence = schedule.next_run_at
        db, _ = _patch(monkeypatch, schedule, queue_raises=ConcurrentTaskLimitError(10))

        assert await schedule_dispatcher._process_one(7, redis=None) is True

        assert _runs(db)[-1].skip_reason is ScheduleSkipReason.CONCURRENCY_LIMIT
        # Past the occurrence just settled, not past *now*: the next grid
        # point can legitimately be minutes ago when catching up.
        assert schedule.next_run_at > occurrence
        assert schedule.consecutive_failures == 0


def _integrity_error(constraint: str | None) -> IntegrityError:
    """An IntegrityError shaped like psycopg's, naming a constraint."""

    class _Diagnostics:
        constraint_name = constraint

    class _Driver(Exception):
        diag = _Diagnostics()

    return IntegrityError("insert", None, _Driver("violation"))


class TestDuplicateOccurrence:
    async def test_advances_instead_of_retrying_forever(self, monkeypatch):
        # The unique index on (schedule_id, scheduled_for) is the backstop
        # behind the row lock. If it ever fires, the occurrence is already
        # recorded — retrying it every tick would wedge the schedule.
        schedule = _schedule()
        occurrence = schedule.next_run_at
        db, calls = _patch(
            monkeypatch,
            schedule,
            queue_raises=_integrity_error("uq_schedule_run_occurrence"),
        )

        assert await schedule_dispatcher._process_one(7, redis=None) is False

        # Past the occurrence just settled, not past *now*: the next grid
        # point can legitimately be minutes ago when catching up.
        assert schedule.next_run_at > occurrence
        assert db.commits == 1

    async def test_a_pause_in_the_window_is_not_overwritten(self, monkeypatch):
        # The failed attempt released the row lock, so the owner can pause in
        # this window; advancing anyway would flip a spent one-shot back to
        # COMPLETED over their decision.
        schedule = _schedule(status=ScheduleStatus.PAUSED)
        before = schedule.next_run_at
        _patch(
            monkeypatch,
            schedule,
            queue_raises=_integrity_error("uq_schedule_run_occurrence"),
        )

        # Locked and found paused before anything is written.
        await schedule_dispatcher._advance_only(7, before)

        assert schedule.status is ScheduleStatus.PAUSED
        assert schedule.next_run_at == before

    async def test_it_advances_from_the_collided_occurrence_not_the_stored_clock(self, monkeypatch):
        # By the time the loser reaches ``_advance_only`` the winner has
        # already advanced the row, so ``next_run_at`` is the *next*
        # occurrence. Anchoring on that would skip it outright — a delivery
        # lost with no history row to explain it.
        occurrence = datetime(2026, 9, 15, 1, 0, tzinfo=UTC)  # 09:00 Shanghai
        advanced = datetime(2026, 9, 16, 1, 0, tzinfo=UTC)  # what the winner wrote
        schedule = _schedule(next_run_at=advanced)
        _patch(monkeypatch, schedule)

        # Still stopped on the occurrence the loser holds? No — the winner
        # moved it. So the loser must write nothing at all.
        await schedule_dispatcher._advance_only(7, occurrence)

        assert schedule.next_run_at == advanced, "the loser overwrote the winner's clock"

    async def test_a_row_that_moved_on_is_left_alone(self, monkeypatch):
        # The owner can edit the recurrence in the window after the failed
        # attempt released the lock. The new clock is theirs, not ours.
        occurrence = datetime(2026, 9, 15, 1, 0, tzinfo=UTC)
        edited = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)  # user moved it to 21:00
        schedule = _schedule(next_run_at=edited)
        db, _ = _patch(monkeypatch, schedule)

        await schedule_dispatcher._settle_skip(
            db, 7, occurrence, ScheduleSkipReason.INSUFFICIENT_CREDITS
        )

        assert schedule.next_run_at == edited
        assert _runs(db) == [], "wrote a skip row against a stale occurrence"
        assert schedule.consecutive_failures == 0

    @pytest.mark.parametrize(
        "constraint",
        ["credit_ledger_idempotency_key_key", None],
        ids=["another-constraint", "driver-names-none"],
    )
    async def test_another_constraint_is_not_claimed_as_a_duplicate(self, monkeypatch, constraint):
        # Any other integrity violation inside the enqueue must not be read
        # as "already recorded": that would advance the clock, write no
        # history row at all, and point the log at the wrong cause.
        schedule = _schedule()
        before = schedule.next_run_at
        db, _ = _patch(monkeypatch, schedule, queue_raises=_integrity_error(constraint))

        assert await schedule_dispatcher._process_one(7, redis=None) is False

        assert schedule.next_run_at == before
        assert db.commits == 0
        assert _runs(db) == []
        assert db.rollbacks == 1


class TestRefusalClassification:
    async def test_a_subclass_is_classified_like_its_parent(self, monkeypatch):
        # ``except _REFUSALS`` catches subclasses, so an exact-type lookup
        # would KeyError the day one of these grows a variant — dropping the
        # occurrence into the generic arm, unrecorded and retried every tick.
        class _PrepaidExhausted(InsufficientCreditsError):
            pass

        schedule = _schedule()
        db, _ = _patch(monkeypatch, schedule, queue_raises=_PrepaidExhausted("gone"))

        assert await schedule_dispatcher._process_one(7, redis=None) is True

        assert _runs(db)[-1].skip_reason is ScheduleSkipReason.INSUFFICIENT_CREDITS
        assert schedule.consecutive_failures == 1


class TestRetireOnce:
    async def test_a_refused_one_shot_is_paused_not_completed(self, monkeypatch):
        # It was turned away, not delivered. COMPLETED would say it ran, and
        # the instant stays put so the history and the date the owner picked
        # still line up.
        # At the end of the catch-up window, where congestion stops being
        # retryable and is finally written into the history.
        occurrence = datetime.now(UTC) - (
            catch_up_grace(ScheduleFrequency.ONCE) - timedelta(seconds=5)
        )
        schedule = _schedule(
            frequency=ScheduleFrequency.ONCE,
            run_date=occurrence.date(),
            next_run_at=occurrence,
        )
        db, _ = _patch(monkeypatch, schedule, queue_raises=ConcurrentTaskLimitError(10))

        await schedule_dispatcher._process_one(7, redis=None)

        assert schedule.status is ScheduleStatus.PAUSED
        assert schedule.next_run_at == occurrence
        assert _runs(db)[-1].skip_reason is ScheduleSkipReason.CONCURRENCY_LIMIT

    async def test_a_one_shot_written_off_as_too_late_is_retired(self, monkeypatch):
        # Its moment genuinely passed; pausing would only invite a resume
        # that cannot be honoured.
        schedule = _schedule(
            frequency=ScheduleFrequency.ONCE,
            run_date=(datetime.now(UTC) - timedelta(hours=3)).date(),
            late_by=timedelta(hours=3),
        )
        _patch(monkeypatch, schedule)

        await schedule_dispatcher._process_one(7, redis=None)

        assert schedule.status is ScheduleStatus.COMPLETED
        assert schedule.next_run_at is None

    async def test_a_spent_one_shot_completes(self, monkeypatch):
        schedule = _schedule(
            frequency=ScheduleFrequency.ONCE,
            run_date=(datetime.now(UTC) - timedelta(minutes=1)).date(),
        )
        _patch(monkeypatch, schedule)

        await schedule_dispatcher._process_one(7, redis=None)

        assert schedule.next_run_at is None
        assert schedule.status is ScheduleStatus.COMPLETED


class TestBackoff:
    def test_a_healthy_tick_waits_exactly_the_interval(self):
        assert schedule_dispatcher._backoff_delay(30, 0) == 30

    def test_it_doubles_per_consecutive_failure(self):
        assert schedule_dispatcher._backoff_delay(30, 1) == 60
        assert schedule_dispatcher._backoff_delay(30, 2) == 120

    def test_it_stops_at_the_ceiling(self):
        assert (
            schedule_dispatcher._backoff_delay(30, 20) == schedule_dispatcher._MAX_BACKOFF_SECONDS
        )

    def test_a_failing_dispatcher_never_polls_faster_than_a_well_one(self):
        # With an interval above the ceiling the inner ``min`` returns less
        # than the healthy interval — a backoff running backwards.
        interval = schedule_dispatcher._MAX_BACKOFF_SECONDS * 2
        for failures in range(1, 6):
            assert schedule_dispatcher._backoff_delay(interval, failures) >= interval


class TestUnresolvableTimezone:
    async def test_pauses_instead_of_raising_every_tick(self, monkeypatch):
        # The API validated the zone against its own tz database; this
        # process may not have the same one. Every path from here runs the
        # recurrence, so the row cannot be advanced — only stopped.
        schedule = _schedule(timezone="Mars/Olympus")
        db, calls = _patch(monkeypatch, schedule)
        before = schedule.next_run_at

        assert await schedule_dispatcher._process_one(7, redis=None) is True

        assert calls["queue"] == []
        assert schedule.status is ScheduleStatus.PAUSED
        # Deliberately not advanced: computing the next occurrence is the
        # thing that cannot be done.
        assert schedule.next_run_at == before
        # The history explains the gap. The row cannot collide with a later
        # run: resuming re-arms the schedule from now, strictly after it.
        (run,) = _runs(db)
        assert run.scheduled_for == before
        assert run.skip_reason is ScheduleSkipReason.MISCONFIGURED
        assert db.commits == 1


class TestSessionTitle:
    """A schedule has no title, so the session each run creates derives one.

    It lands in the sidebar beside typed sessions, and an untitled row there
    is one the owner cannot pick out. Deliberately not the LLM summariser a
    typed session gets: that is a paid call per occurrence, repeated daily
    for months, to name text the user wrote and will recognise.
    """

    def test_a_short_prompt_is_the_title(self):
        assert schedule_dispatcher._session_title("每天总结行业新闻") == "每天总结行业新闻"

    def test_only_the_first_line(self):
        # A prompt is often a paragraph; the sidebar has room for a phrase.
        assert schedule_dispatcher._session_title("总结新闻\n\n要求：\n- 三条以内") == "总结新闻"

    def test_surrounding_whitespace_is_dropped(self):
        assert schedule_dispatcher._session_title("  \n  总结新闻  \n") == "总结新闻"

    def test_a_long_prompt_is_cut_on_a_word_boundary(self):
        prompt = "summarise the industry news and funding rounds for today please"
        title = schedule_dispatcher._session_title(prompt)
        assert title.endswith("…")
        assert len(title) <= schedule_dispatcher._SESSION_TITLE_CHARS + 1
        # Cut between words, so the title reads as a phrase.
        assert not title[:-1].endswith(" ")
        assert prompt.startswith(title[:-1])

    def test_an_early_space_is_not_honoured(self):
        # The word-boundary alignment only looks at the second half. A prompt
        # whose first space sits near the start would otherwise yield a title
        # of a few characters — less useful than a mid-word cut that carries
        # the whole forty.
        prompt = "a" * 19 + " " + "b" * 40
        title = schedule_dispatcher._session_title(prompt)
        assert len(title) == schedule_dispatcher._SESSION_TITLE_CHARS + 1
        assert title.endswith("…")

    def test_a_late_space_is_honoured(self):
        prompt = "a" * 20 + " " + "b" * 40
        assert schedule_dispatcher._session_title(prompt) == "a" * 20 + "…"

    def test_text_without_spaces_still_gets_a_full_length_title(self):
        # CJK has no word spaces. Honouring any space at all would leave a
        # mostly-empty title; honouring none would be a severed word in
        # English. The rule is "only a space near the end counts".
        prompt = "每天早上九点总结行业新闻并且给出三条最重要的结论然后发送给我谢谢另外附上数据来源和链接便于核对"
        title = schedule_dispatcher._session_title(prompt)
        assert len(title) == schedule_dispatcher._SESSION_TITLE_CHARS + 1
        assert title[:-1] == prompt[: schedule_dispatcher._SESSION_TITLE_CHARS]

    def test_a_null_byte_would_have_failed_the_insert(self):
        # ``session.title`` is a ``text`` column and Postgres refuses U+0000
        # in one, so a prompt carrying a null byte would fail the insert and
        # cost the whole occurrence. Models do emit them in garbled output,
        # which a user can paste straight back into a prompt.
        assert schedule_dispatcher._session_title("总结\x00新闻") == "总结新闻"

    def test_other_control_characters_are_dropped(self):
        # A title is rendered; a bell character in the sidebar is noise.
        assert schedule_dispatcher._session_title("a\x00b\x01c\x1fd") == "abcd"

    def test_the_title_always_fits_the_column(self):
        # 40 chars plus an ellipsis, against a 255-char column — checked
        # because the cap lives in this module and the column in another.
        for prompt in ("x" * 10_000, "总结新闻" * 500, "word " * 2_000):
            assert len(schedule_dispatcher._session_title(prompt)) <= 255

    def test_a_prompt_of_format_characters_falls_back_rather_than_raising(self):
        # Refused by the schema now, but rows stored before it was can still
        # reach here. This used to index into an empty list and raise, which
        # the dispatcher retried until the occurrence was written off as
        # ``too_late`` — the wrong reason, for a fault in the prompt.
        for prompt in ("\u200b", "\u200b\n\u200b", "\ufeff"):
            assert (
                schedule_dispatcher._session_title(prompt) == schedule_dispatcher._UNTITLED_SESSION
            )

    def test_format_characters_leading_a_line_do_not_hide_it(self):
        assert schedule_dispatcher._session_title("\u200b\n总结新闻") == "总结新闻"

    def test_a_whitespace_only_prompt_falls_back(self):
        # Unreachable through the schema (``min_length=1`` plus a strip),
        # but the return type is total either way.
        assert (
            schedule_dispatcher._session_title("   \n  ") == schedule_dispatcher._UNTITLED_SESSION
        )
