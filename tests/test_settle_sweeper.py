from sqlalchemy.dialects import postgresql

from app.workers import settle_sweeper


class _Result:
    def all(self) -> list:
        return []


class _CapturingDb:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result()


async def test_list_candidates_filters_before_limit() -> None:
    db = _CapturingDb()

    await settle_sweeper._list_candidates(db)

    assert db.statement is not None
    sql = str(
        db.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "credit_reservation.state = 'OPEN'" in sql
    assert "credit_reservation.session_id IS NULL" in sql
    assert "session.status NOT IN ('IDLE', 'RUNNING')" in sql
    assert "credit_reservation.expires_at < now()" in sql

    where_index = sql.index("WHERE")
    limit_index = sql.index("LIMIT")
    assert where_index < limit_index
