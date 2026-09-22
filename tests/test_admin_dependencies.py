"""The console's request context.

``StaffContext`` is what every admin endpoint resolves; ``caller_ip`` is
what the sign-in refusal log line records.
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


async def test_context_carries_operator_and_session() -> None:
    staff = _staff()
    db = object()

    ctx = await dependencies._authenticated_context(db=db, staff=staff)

    assert ctx.staff is staff
    assert ctx.db is db


# --- caller address ----------------------------------------------------


def test_prefers_the_first_forwarded_hop() -> None:
    # The console sits behind an internal proxy, where the socket peer is
    # the proxy rather than the operator.
    request = _FakeRequest({"x-forwarded-for": "203.0.113.5, 10.0.0.1"})

    assert dependencies.caller_ip(request) == "203.0.113.5"


def test_falls_back_to_the_socket_peer() -> None:
    assert dependencies.caller_ip(_FakeRequest()) == "10.0.0.9"
