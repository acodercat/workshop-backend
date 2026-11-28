"""``PATCH /skills/{id}`` — where each half of the body is written.

The refactor these pin replaced a branch on ``origin`` with one path:
whatever the skill, a user's answer goes to *their* preference row and
never to the shared listing. The regression it guards against is the old
shape creeping back — a ``CUSTOM`` toggle mutating ``skill`` again, which
would work for the owner and be invisible to everyone else.

What that answer now means is narrower: the composer offers the skill.
It no longer decides whether the model sees it, so nothing here is
rationed.

The database is a fake that records statements: what matters here is
*which table each field lands in*, which is a property of the SQL, not
of Postgres.
"""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.dependencies import get_db
from app.api.public import skill as public_skill
from app.auth import authenticate_and_get_user
from app.exception_handlers import install_error_handlers
from app.models.skill import Skill, SkillOrigin, SkillStatus
from app.models.user import User

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_USER_ID = 7


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeDb:
    """Records every statement; answers the one scalar read."""

    def __init__(self, skill: Skill | None, *, preference: bool | None = None):
        self.skill = skill
        self.preference = preference
        self.statements: list[str] = []
        self.commits = 0

    async def execute(self, stmt):
        self.statements.append(_sql(stmt))
        return _FakeResult(self.skill)

    async def scalar(self, stmt):
        self.statements.append(_sql(stmt))
        return self.preference

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj) -> None:
        pass


def _skill(**over) -> Skill:
    skill = Skill(
        **{
            "id": 5,
            "user_id": _USER_ID,
            "origin": SkillOrigin.CUSTOM,
            "name": "notes",
            "display_name": "Notes",
            "description": "takes notes",
            "status": SkillStatus.PUBLISHED,
            "bundle_key": "user_skills/7/x/notes.zip",
            "bundle_size": 10,
            "display_name_i18n": None,
            "category_id": None,
            **over,
        }
    )
    skill.created_at = skill.updated_at = _NOW
    return skill


def _official(**over) -> Skill:
    return _skill(user_id=None, origin=SkillOrigin.OFFICIAL, **over)


def _client(db: _FakeDb) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(public_skill.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[authenticate_and_get_user] = lambda: User(id=_USER_ID, username="u")
    return TestClient(app)


def _written_tables(db: _FakeDb) -> set[str]:
    """Which tables the request wrote to, by the word after INSERT/UPDATE."""
    written = set()
    for statement in db.statements:
        head = statement.split(maxsplit=3)
        if head[:2] == ["INSERT", "INTO"]:
            written.add(head[2])
        elif head[:1] == ["UPDATE"]:
            written.add(head[1])
    return written


@pytest.mark.parametrize("factory", [_skill, _official], ids=["custom", "official"])
def test_the_toggle_writes_only_a_preference_row(factory) -> None:
    """One path, and it never reaches the listing.

    An OFFICIAL row is shared and a CUSTOM one has an audience of one,
    but the answer is the user's in both cases. Writing it to ``skill``
    instead — as the CUSTOM path used to — would look right to the owner
    and be ignored by the worker, which reads the preference row first.
    """
    db = _FakeDb(factory())

    response = _client(db).patch("/skills/5", json={"enabled": True})

    assert response.status_code == 200
    assert _written_tables(db) == {"user_skill_preference"}
    assert db.commits == 1


def test_the_response_reports_the_users_own_answer() -> None:
    db = _FakeDb(_skill(), preference=True)

    body = _client(db).patch("/skills/5", json={"enabled": True}).json()

    assert body["enabled"] is True


def test_a_skill_never_toggled_reports_off() -> None:
    """No preference row is "never said", and that is off — not the
    listing's business and not a default anyone can set."""
    db = _FakeDb(_skill(), preference=None)

    body = _client(db).patch("/skills/5", json={"display_name": "Notes v2"}).json()

    assert body["enabled"] is False


def test_metadata_on_someone_elses_listing_is_refused() -> None:
    """Editing the listing is the owner's alone; the toggle is not."""
    db = _FakeDb(_official())

    response = _client(db).patch("/skills/5", json={"display_name": "Hijacked"})

    assert response.status_code == 403
    assert db.commits == 0


def test_a_rename_clears_the_locale_map() -> None:
    """A map left behind shadows the new name for every locale."""
    db = _FakeDb(_skill(display_name_i18n={"zh-CN": "笔记"}))

    _client(db).patch("/skills/5", json={"display_name": "Notes v2"})

    assert _written_tables(db) == {"skill"}
    (update,) = [statement for statement in db.statements if statement.startswith("UPDATE")]
    assert "display_name_i18n=NULL" in update.replace(" ", "")


def test_turning_one_on_costs_nothing_and_so_is_not_counted() -> None:
    """No quota on the toggle, in either direction.

    The cap this replaces bounded per-turn context: every enabled skill
    spent a metadata line in the ``use_skill`` listing. Enabling now only
    lists the skill in the composer, so there is nothing to ration — and a
    COUNT reappearing here would mean the budget came back with it.
    """
    for enabled in (True, False):
        db = _FakeDb(_skill())

        response = _client(db).patch("/skills/5", json={"enabled": enabled})

        assert response.status_code == 200
        assert all("count(" not in statement for statement in db.statements)
