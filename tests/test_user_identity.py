"""The name pair every console surface renders as one identity.

Two console endpoints resolve an owner for a single row and their list
siblings resolve the same thing through an outer join. What matters is
that the two paths give the *same* answer for the awkward cases, because
a page and the row you clicked through to it would otherwise disagree
about who owns something.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

from typing import NamedTuple

from app.services.user import owner_identity


class _Row(NamedTuple):
    """Shaped like the ``Row`` the query returns: read by label, so a bare
    tuple would not exercise the same code."""

    username: str
    name: str


class _Result:
    def __init__(self, row: _Row | None) -> None:
        self._row = row

    def one_or_none(self) -> _Row | None:
        return self._row


class _FakeDb:
    def __init__(self, row: _Row | None) -> None:
        self._row = row
        self.queries = 0

    async def execute(self, _stmt) -> _Result:
        self.queries += 1
        return _Result(self._row)


async def test_an_owner_resolves_to_both_names() -> None:
    db = _FakeDb(_Row("axon_42", "王小明"))

    assert await owner_identity(db, 42) == ("axon_42", "王小明")
    # One row, not one query per name — the pair is half of one identity.
    assert db.queries == 1


async def test_no_owner_is_answered_without_a_query() -> None:
    """A platform-published skill or an anonymous submission. The list
    queries' outer joins give the same pair of ``None``, which is what
    lets the single-row path and the page agree."""
    db = _FakeDb(_Row("axon_42", "王小明"))

    assert await owner_identity(db, None) == (None, None)
    assert db.queries == 0


async def test_an_owner_the_table_no_longer_holds_reads_as_no_owner() -> None:
    """``user_id`` set, user row gone. Reachable wherever the FK is
    ``SET NULL`` and a read raced the delete, and the branch upstream
    left untested. Answering ``(None, None)`` keeps it identical to the
    outer join's answer rather than raising on a row a page renders
    fine."""
    db = _FakeDb(None)

    assert await owner_identity(db, 42) == (None, None)
