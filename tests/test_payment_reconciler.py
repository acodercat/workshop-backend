"""The payment reconciler's candidate selection, pinned at the SQL level
the way the settle sweeper's is."""

from sqlalchemy.dialects import postgresql

from app.workers import payment_reconciler


class _Result:
    def all(self) -> list:
        return []


class _CapturingDb:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result()


async def test_list_candidates_takes_pending_least_recently_attempted_first() -> None:
    """Orders the provider has no verdict on yet stay PENDING for up to a
    day. Taken oldest-first, a full batch of them would stand between the
    poller and every newer order — including a paid one whose notification
    was lost — until they aged out. Never-attempted first, then least
    recently attempted, so every PENDING order gets its turn."""
    db = _CapturingDb()

    await payment_reconciler._list_candidates(db)

    assert db.statement is not None
    sql = str(
        db.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "payment_order.status = 'PENDING'" in sql
    assert "payment_order.created_at <" in sql
    assert "ORDER BY payment_order.reconcile_attempted_at ASC NULLS FIRST" in sql
    assert "expires_at" not in sql
    assert sql.index("WHERE") < sql.index("ORDER BY") < sql.index("LIMIT")
