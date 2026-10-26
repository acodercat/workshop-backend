"""The console's request context.

``build_context`` is what every admin endpoint's ``StaffContext`` resolves
through; the correlation id and caller address it captures are what make
an audit row traceable back to a request, so losing them silently is the
failure asserted against here.
"""

from app.api.admin import dependencies
from app.models.staff import Staff, StaffStatus


class _FakeClient:
    host = "10.0.0.9"


class _FakeState:
    request_id = "req-123"


class _FakeRequest:
    def __init__(self, headers: dict | None = None) -> None:
        self.headers = headers or {}
        self.state = _FakeState()
        self.client = _FakeClient()


def _staff() -> Staff:
    return Staff(
        id=7,
        username="alice",
        password_hash="x",
        name="Alice",
        status=StaffStatus.ACTIVE,
        token_version=0,
    )


def test_context_carries_operator_session_and_request_metadata() -> None:
    staff = _staff()
    db = object()

    ctx = dependencies.build_context(_FakeRequest(), db=db, staff=staff)

    assert ctx.staff is staff
    assert ctx.db is db
    assert ctx.request_id == "req-123"
    assert ctx.ip_address == "10.0.0.9"


# --- caller address ----------------------------------------------------


def test_prefers_the_first_forwarded_hop() -> None:
    # The console sits behind an internal proxy, where the socket peer is
    # the proxy rather than the operator.
    request = _FakeRequest({"x-forwarded-for": "203.0.113.5, 10.0.0.1"})

    assert dependencies.caller_ip(request) == "203.0.113.5"


def test_falls_back_to_the_socket_peer() -> None:
    assert dependencies.caller_ip(_FakeRequest()) == "10.0.0.9"


def test_a_forwarded_hop_that_is_not_an_address_falls_back_to_the_peer() -> None:
    # The column is String(45) and commits with the operator's action, so an
    # unparsed hop longer than that would abort the action itself.
    for hop in ("unknown", "a" * 46):
        request = _FakeRequest({"x-forwarded-for": hop})

        assert dependencies.caller_ip(request) == "10.0.0.9"
