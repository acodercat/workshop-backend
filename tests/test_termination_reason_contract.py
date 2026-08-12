"""The backend's half of the ``termination_reason`` contract, pinned to literals.

``app/models/session_turn.py`` and the worker's
``agent_worker/infra/database.py`` declare this enum independently — separate
services, separate venvs, no shared module to import. The worker is the only
writer; this service is the only reader, and it owns the migration.

**Nothing stops a divergence at the point it happens.** The column is a plain
``VARCHAR(32)`` on purpose (``native_enum=False``, ``create_constraint=False``,
so a new reason is a code change rather than a migration), which means a worker
writing a reason declared only on its side commits perfectly. The failure lands
here: the ORM raises when it loads that row — the session detail endpoint, the
admin console — and the row is unreadable by the service that owns the table.

Dropping a member is the other direction and no safer. A worker still deployed
against the old code goes on writing it, and every row it writes becomes
unreadable the moment this side stops declaring it. So the deploy order that
works is: add here first, remove here last.

Same shape as ``tests/test_redis_key_contract.py`` and for the same reason. The
mirror is ``agent-worker/tests/infra/test_termination_reason_contract.py``,
carrying these same values.
"""

from app.models.session_turn import SessionTurn, TerminationReason

#: The agreement, as it appears in the column. Mirrored in the worker file
#: named above. Order is not part of the contract; membership and spelling are.
AGREED_REASONS = {
    "completed",
    "user_cancelled",
    "insufficient_credits",
    "worker_shutdown",
    "model_error",
    "timeout",
    "empty_output",
    "internal_error",
    "worker_lost",
}

#: ``session_turn.termination_reason`` is declared ``length=32``.
MAX_REASON_LENGTH = 32


def test_the_reasons_are_the_agreed_set():
    actual = {reason.value for reason in TerminationReason}
    added = actual - AGREED_REASONS
    removed = AGREED_REASONS - actual
    assert not (added or removed), (
        f"termination_reason drifted: added {sorted(added)}, removed "
        f"{sorted(removed)}. The worker declares the same enum in "
        f"agent_worker/infra/database.py and is the only thing that writes "
        f"this column. Change it there in the same breath, and its mirror test "
        f"with it — adding here first and removing here last, so no deployed "
        f"worker is ever writing a reason this side cannot read."
    )


def test_the_reasons_fit_the_column():
    too_long = sorted(r for r in AGREED_REASONS if len(r) > MAX_REASON_LENGTH)
    assert not too_long, (
        f"{too_long} exceed the column's {MAX_REASON_LENGTH} characters and "
        f"would be truncated on write, so the reason read back is not the one "
        f"the worker recorded."
    )


def test_the_column_still_holds_what_the_contract_assumes():
    """The length above is only true while the column says so.

    Widening it is fine and needs a migration; narrowing it silently would
    make the per-reason check above pass while the database truncates.
    """
    column = SessionTurn.__table__.c.termination_reason
    assert column.type.length == MAX_REASON_LENGTH, (
        f"termination_reason is now VARCHAR({column.type.length}); this "
        f"contract and the worker's mirror both assume {MAX_REASON_LENGTH}."
    )
