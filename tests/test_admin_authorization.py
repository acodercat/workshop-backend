"""The console's role ladder.

Two layers, both cheap and both load-bearing:

- :meth:`StaffRole.covers` — the ladder itself.
- the dependency :func:`app.api.admin.dependencies._require` builds — that
  a sufficient role yields a context stamped with the request metadata,
  and an insufficient one raises 403 rather than 401.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import pytest

from app.api.admin import dependencies
from app.errors import StaffForbiddenError
from app.models.staff import Staff, StaffRole, StaffStatus

ALL_ROLES = [StaffRole.VIEWER, StaffRole.OPERATOR, StaffRole.OWNER]


def _staff(role: StaffRole) -> Staff:
    return Staff(
        id=7,
        username="alice",
        password_hash="x",
        display_name="Alice",
        role=role,
        status=StaffStatus.ACTIVE,
        token_version=0,
    )


class _FakeClient:
    host = "10.0.0.9"


class _FakeState:
    request_id = "req-123"


class _FakeRequest:
    def __init__(self, headers: dict | None = None) -> None:
        self.headers = headers or {}
        self.state = _FakeState()
        self.client = _FakeClient()


# --- the ladder --------------------------------------------------------


@pytest.mark.parametrize("role", ALL_ROLES)
def test_every_role_covers_itself(role: StaffRole) -> None:
    assert role.covers(role)


def test_higher_roles_cover_lower_ones() -> None:
    assert StaffRole.OWNER.covers(StaffRole.OPERATOR)
    assert StaffRole.OWNER.covers(StaffRole.VIEWER)
    assert StaffRole.OPERATOR.covers(StaffRole.VIEWER)


def test_lower_roles_do_not_cover_higher_ones() -> None:
    assert not StaffRole.VIEWER.covers(StaffRole.OPERATOR)
    assert not StaffRole.VIEWER.covers(StaffRole.OWNER)
    assert not StaffRole.OPERATOR.covers(StaffRole.OWNER)


@pytest.mark.parametrize("role", ALL_ROLES)
def test_every_role_is_ranked(role: StaffRole) -> None:
    # A member added to the enum but not to ``_ROLE_RANK`` would raise
    # KeyError on its first authorization check — in production, on the
    # request that used it.
    assert role.covers(StaffRole.VIEWER) in (True, False)


# --- the dependency ----------------------------------------------------


async def test_sufficient_role_yields_a_context_with_request_metadata() -> None:
    dependency = dependencies._require(StaffRole.OPERATOR)
    staff = _staff(StaffRole.OWNER)
    db = object()

    ctx = await dependency(request=_FakeRequest(), db=db, staff=staff)

    assert ctx.staff is staff
    assert ctx.db is db
    # The correlation id and caller address are what make an audit row
    # traceable back to a request; losing them silently is the failure
    # this asserts against.
    assert ctx.request_id == "req-123"
    assert ctx.ip_address == "10.0.0.9"


async def test_insufficient_role_is_forbidden_not_unauthenticated() -> None:
    # 403, never 401: the caller *is* signed in, and the console has to
    # tell "session expired" apart from "you can't do this".
    dependency = dependencies._require(StaffRole.OWNER)

    with pytest.raises(StaffForbiddenError) as exc_info:
        await dependency(
            request=_FakeRequest(), db=object(), staff=_staff(StaffRole.OPERATOR)
        )

    assert exc_info.value.status_code == 403
    # The message names the role needed, so the console can say which.
    assert StaffRole.OWNER.value in exc_info.value.message


async def test_viewer_gate_admits_the_lowest_role() -> None:
    dependency = dependencies._require(StaffRole.VIEWER)

    ctx = await dependency(
        request=_FakeRequest(), db=object(), staff=_staff(StaffRole.VIEWER)
    )

    assert ctx.staff.role is StaffRole.VIEWER


# --- caller address ----------------------------------------------------


def test_prefers_the_first_forwarded_hop() -> None:
    # The console sits behind an internal proxy, where the socket peer is
    # the proxy rather than the operator.
    request = _FakeRequest({"x-forwarded-for": "203.0.113.5, 10.0.0.1"})

    assert dependencies.caller_ip(request) == "203.0.113.5"


def test_falls_back_to_the_socket_peer() -> None:
    assert dependencies.caller_ip(_FakeRequest()) == "10.0.0.9"


def test_a_hop_that_is_not_an_address_falls_back_to_the_peer() -> None:
    # The audit row commits in the same transaction as the action it
    # describes, and ``ip_address`` is ``String(45)`` — an unparsed header
    # this long would not make one row less informative, it would abort
    # the operator's own action. Parsing is what makes that impossible.
    request = _FakeRequest({"x-forwarded-for": "x" * 60 + ", 10.0.0.1"})

    assert dependencies.caller_ip(request) == "10.0.0.9"


def test_an_obfuscated_hop_falls_back_to_the_peer() -> None:
    # RFC 7239 lets a proxy write ``unknown`` for a hop it will not name;
    # that is not an address and must not be stored as one.
    request = _FakeRequest({"x-forwarded-for": "unknown, 10.0.0.1"})

    assert dependencies.caller_ip(request) == "10.0.0.9"


def test_an_ipv6_hop_is_stored_canonically() -> None:
    # Parsing normalises, so one address spelled two ways cannot read as
    # two callers in the trail — and every stored form fits ``String(45)``
    # by construction.
    request = _FakeRequest({"x-forwarded-for": "2001:0DB8:0000::0001"})

    assert dependencies.caller_ip(request) == "2001:db8::1"


def test_no_header_and_no_socket_peer_is_none() -> None:
    request = _FakeRequest()
    request.client = None

    assert dependencies.caller_ip(request) is None
