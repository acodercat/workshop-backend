"""The query shapes and error contracts behind the /api/schedules surface.

Only the ones carrying an invariant a reader cannot see from the call site.
The endpoints' CRUD paths are exercised end-to-end against a live stack;
what is pinned here is the predicate that decides what counts as news,
because the count and the mark-read must agree and there is no way to tell
from either endpoint alone that they do.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql

from app.api.public import schedule as endpoint
from app.errors import (
    AppError,
    FileNotInSessionError,
    FileNotOwnedError,
    FileNotUploadedError,
    FileTooLargeError,
)
from app.models.schedule import ScheduleSkipReason
from app.models.session_turn import SessionTurnStatus, TerminationReason


def _sql(statement) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


class TestUnreadPredicate:
    """``_unread`` is the single source both badge endpoints read.

    A mark-read wider than the count would stamp ``read_at`` on an
    occurrence whose turn was still running; once it finished, the badge
    would never mention it. The two cannot drift while they share this.
    """

    def test_counts_only_occurrences_with_an_outcome(self):
        sql = _sql(endpoint._unread(1))
        assert "schedule_run.read_at IS NULL" in sql
        # A skipped occurrence is news at once.
        assert "schedule_run.skip_reason IS NOT NULL" in sql
        # One that ran is news once the turn the dispatcher queued has ended
        # — not the session's latest status, which the owner's own later
        # messages move.
        assert "first_turn.status IN ('COMPLETE', 'FAILED', 'CANCELLED')" in sql

    def test_a_deleted_session_is_not_news(self):
        # The user closed that book; badging it would reopen it for them.
        assert "session.deleted_at IS NULL" in _sql(endpoint._unread(1))

    def test_it_is_scoped_to_one_owner(self):
        assert "schedule_run.user_id = 7" in _sql(endpoint._unread(7))

    def test_the_joins_are_outer(self):
        # An inner join would silently drop every skipped occurrence — the
        # ones with no session are exactly what the badge is for.
        sql = _sql(endpoint._unread(1))
        assert "LEFT OUTER JOIN session" in sql
        assert "LEFT OUTER JOIN LATERAL" in sql


class TestAcknowledgingWhatTheListObserved:
    """Opening the page marks as seen the settled results the list request
    observed — older runs included, never one it saw still running.

    Not "everything settled before some time": a turn stamps ``terminated_at``
    when its transaction starts and becomes visible only at commit, so a
    cutoff taken in between covers a result the request still saw running.
    """

    async def test_the_unread_ids_are_read_before_the_schedules(self, monkeypatch):
        # Each statement sees what had committed when it started, so what the
        # first saw settled the second sees settled too. Reversed, an id could
        # name a result the list rendered as still running.
        order: list[str] = []

        class _Db:
            async def execute(self, statement):
                order.append("unread")

                class _Result:
                    @staticmethod
                    def scalars():
                        return iter([11, 12])

                return _Result()

        async def schedules(_db, _user_id):
            order.append("schedules")
            return []

        monkeypatch.setattr(endpoint, "_schedule_responses", schedules)
        from types import SimpleNamespace

        listing = await endpoint.list_schedules(_Db(), SimpleNamespace(id=7))

        assert order == ["unread", "schedules"]
        assert listing.unread_run_ids == [11, 12]

    async def test_mark_read_touches_only_the_given_ids_of_the_caller(self):
        from app.schemas.schedule import MarkRunsReadRequest

        statements = []

        class _Db:
            async def execute(self, statement):
                statements.append(statement)

            async def commit(self):
                pass

        from types import SimpleNamespace

        await endpoint.mark_runs_read(
            MarkRunsReadRequest(run_ids=[3, 4]), _Db(), SimpleNamespace(id=7)
        )

        (update,) = statements
        sql = _sql(update)
        assert "schedule_run.id IN (3, 4)" in sql
        # The ids came from a response, but the body is the caller's to write.
        assert "schedule_run.user_id = 7" in sql
        assert "schedule_run.read_at IS NULL" in sql

    async def test_nothing_to_acknowledge_is_not_a_write(self):
        from types import SimpleNamespace

        from app.schemas.schedule import MarkRunsReadRequest

        class _Never:
            async def execute(self, statement):
                raise AssertionError("wrote with no ids")

        await endpoint.mark_runs_read(
            MarkRunsReadRequest(run_ids=[]), _Never(), SimpleNamespace(id=7)
        )

    def test_the_id_list_is_bounded_both_ways(self):
        from pydantic import ValidationError

        from app.schemas.schedule import UNREAD_ACKNOWLEDGE_MAX, MarkRunsReadRequest

        with pytest.raises(ValidationError):
            MarkRunsReadRequest(run_ids=list(range(UNREAD_ACKNOWLEDGE_MAX + 1)))


def test_the_badge_counts_with_the_same_predicate_the_listing_acknowledges():
    """Structural, not behavioural: neither builds its own filter."""
    import inspect

    for handler in (endpoint.count_unread_runs, endpoint.list_schedules):
        assert "_unread(" in inspect.getsource(handler)


class TestRefusalContracts:
    """``error_type`` is the stable key the frontend localizes on.

    These three refusals earn a typed error rather than a bare
    ``HTTPException`` because the status-derived fallbacks (``forbidden``,
    ``bad_request``) either have no catalog entry — landing on "something
    went wrong, try again later" — or say nothing about what to do. All
    three have a specific next action, and none of them is waiting.
    """

    def test_the_caps_carry_their_number(self):
        from app.errors import ScheduleActiveLimitError, ScheduleLimitError

        for error_class, expected_type in (
            (ScheduleActiveLimitError, "schedule_active_limit"),
            (ScheduleLimitError, "schedule_limit"),
        ):
            exc = error_class(5)
            assert exc.error_type == expected_type
            # The localized sentence names the limit, so it has to travel.
            assert exc.details == {"limit": 5}
            assert exc.status_code == 403

    def test_a_past_run_time_is_its_own_kind(self):
        from app.errors import ScheduleRunTimePastError

        exc = ScheduleRunTimePastError("gone")
        assert exc.error_type == "schedule_run_time_past"
        assert exc.status_code == 400

    def test_the_endpoint_validates_the_request_before_the_account(self):
        # Being told "you are at your limit" about a request that was never
        # going to be accepted reads as the wrong problem.
        import inspect

        source = inspect.getsource(endpoint.create_schedule)
        assert source.index("lifecycle.arm") < source.index("_assert_row_headroom")


class TestDeterministicPaging:
    """The run history pages on more than a timestamp.

    Two occurrences can share a ``created_at`` (the dispatcher writes a
    batch in one tick), and a sort that ends on a non-unique column lets
    Postgres return them in any order per query — so a row can appear on
    both page 1 and page 2, or on neither. The console lists learned this
    the hard way; ``tests/test_admin_list_ordering.py`` guards that surface,
    which does not reach this one.
    """

    def test_the_sort_ends_on_a_unique_column(self):
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint.list_schedule_runs)))
        order_by = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "order_by"
        ]
        assert order_by, "the run history paginates without an explicit sort"
        # The *paginated* sort, not simply the last ``order_by`` in the
        # function: the endpoint also sorts a one-row lateral subquery, and
        # picking by position broke the moment that was added. Identify it by
        # what it sorts — the rows being paged are ``ScheduleRun``.
        paged = [call for call in order_by if ast.unparse(call.args[-1]).startswith("ScheduleRun.")]
        # This count is the brittle half of the guard, and it is deliberately
        # kept: a second ``ScheduleRun`` sort would mean two candidate paging
        # orders and the test could no longer tell which one paginates. If you
        # arrived here after a refactor that legitimately added one, assert on
        # the compiled SQL instead of the source AST — that needs the query
        # lifted out of the handler, which is why it is not done here.
        assert len(paged) == 1, "expected exactly one ScheduleRun ordering"
        # Ends on the primary key, so the order is total: two occurrences can
        # share a ``created_at`` (a batch written in one tick), and a
        # non-unique sort lets a row appear on two pages or neither.
        assert ast.unparse(paged[0].args[-1]).startswith("ScheduleRun.id")


class TestDeleteRacesDispatch:
    """Deleting a schedule the dispatcher is mid-flight on is safe by lock.

    ``delete_schedule`` reads the row unlocked and then deletes it, which
    looks like a race against ``_process_one``'s ``FOR UPDATE`` — but the
    DELETE itself needs the row lock, so it queues behind the dispatcher and
    lands only once that transaction ends. Verified live: the occurrence
    either completes and the history row goes with the schedule (CASCADE),
    or the delete wins first and there was nothing to dispatch. No orphan
    history, no session without a schedule.

    Pinned here because the safety is not visible at either call site — it
    lives in the lock ordering, and adding a ``SKIP LOCKED`` or dropping the
    ``FOR UPDATE`` would quietly remove it.
    """

    def test_the_dispatcher_locks_the_row_it_acts_on(self):
        import inspect

        from app.workers import schedule_dispatcher

        assert "with_for_update" in inspect.getsource(schedule_dispatcher._lock)
        # Not SKIP LOCKED: a delete that skipped the lock would commit
        # underneath the dispatcher instead of waiting for it.
        assert "skip_locked" not in inspect.getsource(schedule_dispatcher._lock).lower()

    def test_history_cascades_from_the_schedule(self):
        from app.models.schedule import ScheduleRun

        fk = next(iter(ScheduleRun.__table__.c.schedule_id.foreign_keys))
        assert fk.ondelete == "CASCADE"


class TestEditsLockTheRow:
    """An edit decides on the row under the lock the dispatcher settles under.

    Unlocked, a PATCH could read an ACTIVE one-shot, lose the race to the
    dispatcher completing it, and then store a future next run on a row now
    COMPLETED — saved, shown, and never fired, because the dispatcher scans
    ACTIVE rows only.
    """

    class _CapturingDb:
        statement = None

        async def execute(self, statement):
            self.statement = statement

            class _Result:
                @staticmethod
                def scalar_one_or_none():
                    return object()

            return _Result()

    async def test_the_edit_loads_its_row_for_update(self):
        import inspect

        source = inspect.getsource(endpoint.update_schedule)
        assert "for_update=True" in source
        # And before it decides anything about the status or the clock.
        assert source.index("for_update=True") < source.index("lifecycle.apply_owner_edit(")

    async def test_for_update_is_what_the_lookup_sends(self):
        db = self._CapturingDb()
        await endpoint._get_or_404(db, 3, 7, for_update=True)
        sql = _sql(db.statement)
        assert sql.endswith("FOR UPDATE")
        assert "schedule.user_id = 7" in sql

    async def test_a_plain_lookup_takes_no_lock(self):
        db = self._CapturingDb()
        await endpoint._get_or_404(db, 3, 7)
        assert "FOR UPDATE" not in _sql(db.statement)


class TestRunResponse:
    """One occurrence as the history and the card both render it."""

    @staticmethod
    def _row(
        *,
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        skip_reason=None,
        session_deleted_at=None,
        status=None,
        termination_reason=None,
        started_at=None,
        ended_at=None,
    ):
        from types import SimpleNamespace

        from app.models.schedule import ScheduleRun

        at = datetime(2026, 9, 11, 1, 0, tzinfo=UTC)
        run = ScheduleRun(
            id=1,
            schedule_id=1,
            user_id=1,
            scheduled_for=at,
            session_id=session_id,
            skip_reason=skip_reason,
            read_at=None,
            created_at=at,
        )
        return SimpleNamespace(
            ScheduleRun=run,
            session_deleted_at=session_deleted_at,
            status=status,
            termination_reason=termination_reason,
            started_at=started_at,
            ended_at=ended_at,
        )

    def test_a_run_reports_its_first_turn(self):
        response = endpoint._run_response(self._row(status=SessionTurnStatus.FAILED))
        assert response.turn_status is SessionTurnStatus.FAILED
        assert response.session_id is not None
        assert response.skip_reason is None

    def test_a_skip_reports_only_its_reason(self):
        response = endpoint._run_response(
            self._row(session_id=None, skip_reason=ScheduleSkipReason.TOO_LATE)
        )
        assert response.skip_reason is ScheduleSkipReason.TOO_LATE
        assert response.turn_status is None
        assert response.session_id is None

    def test_a_deleted_session_keeps_its_outcome_but_not_its_link(self):
        # The session endpoint refuses a deleted session, so a link to it is
        # a dead end; what the run did is still true.
        response = endpoint._run_response(
            self._row(
                status=SessionTurnStatus.COMPLETE,
                session_deleted_at=datetime(2026, 9, 12, tzinfo=UTC),
            )
        )
        assert response.session_id is None
        assert response.turn_status is SessionTurnStatus.COMPLETE

    def test_completed_is_dropped_as_saying_nothing_new(self):
        response = endpoint._run_response(
            self._row(
                status=SessionTurnStatus.COMPLETE,
                termination_reason=TerminationReason.COMPLETED,
            )
        )
        assert response.termination_reason is None

    def test_a_reason_that_tells_failures_apart_survives(self):
        for reason in (TerminationReason.TIMEOUT, TerminationReason.MODEL_ERROR):
            response = endpoint._run_response(
                self._row(status=SessionTurnStatus.FAILED, termination_reason=reason)
            )
            assert response.termination_reason is reason

    def test_duration_is_read_by_label(self):
        started = datetime(2026, 9, 11, 1, 0, tzinfo=UTC)
        response = endpoint._run_response(
            self._row(
                status=SessionTurnStatus.COMPLETE,
                started_at=started,
                ended_at=started + timedelta(seconds=42),
            )
        )
        assert response.duration_seconds == 42.0


class TestLatestRun:
    def test_it_reads_through_the_same_run_query_as_the_history(self):
        # The card and the history are two views of one occurrence; built by
        # two queries they could disagree about it.
        import inspect

        for reader in (endpoint._schedule_responses, endpoint.list_schedule_runs):
            source = inspect.getsource(reader)
            assert "_runs(" in source
            assert "_run_response(" in source

    def test_the_list_is_read_back_from_the_database(self):
        # This session does not expire on commit; without populate_existing
        # an edit's response would echo the object as the request left it.
        import inspect

        assert "populate_existing=True" in inspect.getsource(endpoint._schedule_responses)


class TestRunDuration:
    def test_a_finished_turn_reports_its_seconds(self):
        started = datetime(2026, 9, 11, 1, 0, tzinfo=UTC)
        assert endpoint._duration(started, started + timedelta(seconds=95)) == 95.0

    def test_an_in_flight_turn_has_no_duration(self):
        # 1441 such rows exist — a turn still running has no end yet.
        started = datetime(2026, 9, 11, 1, 0, tzinfo=UTC)
        assert endpoint._duration(started, None) is None
        assert endpoint._duration(None, started) is None

    def test_a_backwards_clock_reports_nothing_rather_than_a_negative(self):
        # Both timestamps are ``func.now()`` on whichever host wrote them, so
        # a backwards step is a clock disagreement, not a run that took no
        # time. Clamping to 0 would assert an instantaneous run; None is the
        # honest answer and renders as nothing.
        started = datetime(2026, 9, 11, 1, 0, tzinfo=UTC)
        assert endpoint._duration(started, started - timedelta(seconds=5)) is None

    def test_zero_is_a_duration_and_survives(self):
        started = datetime(2026, 9, 11, 1, 0, tzinfo=UTC)
        assert endpoint._duration(started, started) == 0.0


class TestFileErrorStatuses:
    """A rejected attachment must not read as a platform fault.

    The three file errors are ``AppError``s: the shared handler turns each
    into its status and a stable ``error_type`` the client localizes, so no
    endpoint maps them itself. That contract is what these pin.
    """

    @pytest.mark.parametrize(
        ("exc", "status_code", "error_type"),
        [
            (FileNotOwnedError("not yours"), 403, "file_not_owned"),
            (FileNotInSessionError("not bound"), 403, "file_not_in_session"),
            (FileNotUploadedError("never arrived"), 400, "file_not_uploaded"),
            (FileTooLargeError(9, 5), 413, "file_too_large"),
        ],
        ids=["another-users-key", "not-bound-here", "upload-never-completed", "over-the-cap"],
    )
    def test_each_is_a_typed_app_error(self, exc, status_code, error_type):
        assert isinstance(exc, AppError)
        assert exc.status_code == status_code
        assert exc.error_type == error_type

    def test_too_large_carries_both_numbers(self):
        # The localized sentence says how far over; without the numbers it
        # can only say "too large", which the owner already knew.
        assert FileTooLargeError(9, 5).details == {"size_bytes": 9, "max_bytes": 5}
