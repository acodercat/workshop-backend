from datetime import datetime, timedelta, timezone

from app.services.credits import settle


class _Result:
    def __init__(self, row) -> None:
        self._row = row

    def one_or_none(self):
        return self._row


class _Db:
    def __init__(self, row) -> None:
        self._row = row
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result(self._row)


async def test_read_usage_returns_tokens_and_turn_seconds() -> None:
    created_at = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    terminated_at = created_at + timedelta(seconds=42.5)
    db = _Db((123, 456, created_at, terminated_at))

    usage = await settle._read_usage_and_duration_from_session_turn(db, 99)

    assert usage == (123, 456, 42.5)


async def test_read_usage_uses_zero_duration_for_legacy_rows() -> None:
    created_at = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    db = _Db((123, 456, created_at, None))

    usage = await settle._read_usage_and_duration_from_session_turn(db, 99)

    assert usage == (123, 456, 0.0)


async def test_read_usage_returns_none_when_tokens_are_missing() -> None:
    created_at = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    terminated_at = created_at + timedelta(seconds=42.5)
    db = _Db((None, 456, created_at, terminated_at))

    usage = await settle._read_usage_and_duration_from_session_turn(db, 99)

    assert usage is None


