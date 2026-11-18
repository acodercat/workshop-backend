"""Schema shape for scheduled tasks.

Three of these indexes and constraints are load-bearing rather than
cosmetic — lose one and the failure is silent (a task runs twice, the
dispatcher seq-scans, a nonsense recurrence becomes storable). So each is
pinned here by predicate, not just by name.
"""

from sqlalchemy import CheckConstraint, Index
from sqlalchemy.dialects import postgresql

from app.models.schedule import Schedule, ScheduleRun, ScheduleSkipReason
from app.models.schedule_mcp_server import ScheduleMCPServer


def _indexes(model) -> dict[str, Index]:
    return {item.name: item for item in model.__table__.indexes if isinstance(item, Index)}


def _checks(model) -> dict[str, str]:
    return {
        item.name: str(
            item.sqltext.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        for item in model.__table__.constraints
        if isinstance(item, CheckConstraint) and item.name
    }


def _predicate(index: Index) -> str:
    return str(
        index.dialect_options["postgresql"]["where"].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


class TestDispatcherScan:
    def test_due_index_is_partial_on_active(self):
        # Without the partial predicate the dispatcher's every-30s scan
        # walks paused and completed rows too — which on a long-lived
        # deployment is most of the table and none of it ever due.
        index = _indexes(Schedule)["ix_schedule_due"]
        assert [c.name for c in index.columns] == ["next_run_at"]
        assert _predicate(index) == "status = 'ACTIVE'"


class TestIdempotency:
    def test_occurrence_index_is_unique_on_schedule_and_instant(self):
        # This is what makes dispatch at-most-once across restarts,
        # replicas and clock drift. A non-unique index here would let a
        # crash-and-retry run the user's task a second time.
        index = _indexes(ScheduleRun)["uq_schedule_run_occurrence"]
        assert index.unique is True
        assert [c.name for c in index.columns] == ["schedule_id", "scheduled_for"]

    def test_unread_index_is_partial_on_unread(self):
        index = _indexes(ScheduleRun)["ix_schedule_run_unread"]
        assert _predicate(index) == "read_at IS NULL"


class TestRecurrenceInvariants:
    def test_each_frequency_requires_exactly_its_own_columns(self):
        # The recurrence math asserts on these columns rather than
        # inventing a fallback, so a WEEKLY row with no weekday must be
        # unstorable — the alternative is a crash in a background process
        # at whatever hour the row was due.
        predicate = _checks(Schedule)["ck_schedule_frequency_fields"]
        for frequency in ("ONCE", "DAILY", "WEEKLY", "MONTHLY"):
            assert f"frequency = '{frequency}'" in predicate
        assert "run_date IS NOT NULL" in predicate
        assert "weekday IS NOT NULL" in predicate
        assert "day_of_month IS NOT NULL" in predicate

    def test_day_of_month_spans_a_full_calendar_month(self):
        # 1..31. The "what does the 31st mean in February" question is
        # answered by the recurrence rather than by narrowing the column:
        # a month too short for the chosen day does not fire.
        assert (
            "day_of_month BETWEEN 1 AND 31" in _checks(Schedule)["ck_schedule_day_of_month_range"]
        )

    def test_weekday_is_a_valid_weekday(self):
        assert "weekday BETWEEN 0 AND 6" in _checks(Schedule)["ck_schedule_weekday_range"]


class TestRunOutcome:
    def test_a_run_cannot_both_enqueue_and_be_skipped(self):
        # ``session_id`` non-NULL means it ran; ``skip_reason`` non-NULL
        # means nothing was spent. Both set would make the history view's
        # rendering order the source of truth.
        predicate = _checks(ScheduleRun)["ck_schedule_run_outcome"]
        assert "session_id IS NOT NULL" in predicate
        assert "skip_reason IS NOT NULL" in predicate


class TestConnectorBinding:
    """``schedule_mcp_server`` — why it is a table and not an id array."""

    def test_a_deleted_connector_unbinds_itself(self):
        # The whole reason this is not an array on ``schedule``: removing a
        # Connector has to remove it from every schedule that named it, and
        # only the database can promise that.
        fks = {
            fk.column.table.name: fk.ondelete
            for fk in ScheduleMCPServer.__table__.foreign_keys  # type: ignore[attr-defined]
        }
        assert fks == {"schedule": "CASCADE", "mcp_server": "CASCADE"}

    def test_the_cascade_from_mcp_server_has_an_index(self):
        # The composite PK leads with ``schedule_id``, so it cannot serve
        # the reference check Postgres runs when an ``mcp_server`` row is
        # deleted. Without this index that check seq-scans.
        index = _indexes(ScheduleMCPServer)["ix_schedule_mcp_server_mcp_server_id"]
        assert [c.name for c in index.columns] == ["mcp_server_id"]

    def test_one_connector_cannot_be_bound_twice(self):
        pk = ScheduleMCPServer.__table__.primary_key  # type: ignore[attr-defined]
        assert [c.name for c in pk.columns] == ["schedule_id", "mcp_server_id"]


class TestSkillPicks:
    def test_no_skills_is_an_empty_array_never_null(self):
        # Readers iterate without a null-guard, matching
        # ``session_turn.skills``. A NULL here would be a crash at every
        # firing of a schedule created before the column existed.
        column = Schedule.__table__.c.skills  # type: ignore[attr-defined]
        assert not column.nullable
        assert "'[]'::jsonb" in str(column.server_default.arg)


class TestSkipReasonConsequences:
    """What each skip reason does to its schedule — the policy, stated once.

    The dispatcher applies these through ``_record`` and the frontend mirrors
    ``counts_as_failure`` to decide what is painted red, so a change here is a
    change to both.
    """

    def test_only_what_the_owner_must_fix_counts_as_a_failure(self):
        counted = {reason for reason in ScheduleSkipReason if reason.counts_as_failure}
        assert counted == {
            ScheduleSkipReason.INSUFFICIENT_CREDITS,
            ScheduleSkipReason.MISCONFIGURED,
        }

    def test_only_lateness_retires_one_shot(self):
        # Every other cause can clear, so the one-shot waits to be re-armed.
        retiring = {reason for reason in ScheduleSkipReason if reason.retires_one_shot}
        assert retiring == {ScheduleSkipReason.TOO_LATE}
