"""The Meeting integration, driven through a fake Meeting over ``httpx``.

What these protect is mostly invisible when it breaks: a session cookie
riding along on another user's request, a rotated passport dropped, a
refusal Meeting buries in a 200 read as success, an outage read as a
sign-out.
"""

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.errors import (
    AccountFrozenError,
    AccountLockedError,
    IdentityProviderRateLimitedError,
    IdentityProviderRejectedError,
    IdentityProviderUnavailableError,
    InvalidCredentialsError,
    NamePolicyError,
    PasswordPolicyError,
)
from app.integrations.meeting.accounts import MeetingAccounts
from app.integrations.meeting.transport import MeetingSession, MeetingTransport, open_meeting_http
from app.services.identity.provider import CredentialRejected, MeetingIdentityProvider

_ORIGIN = "https://meeting.test"
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_PEM = (
    _PRIVATE_KEY.public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)


def _ok(data=None, *, passport: str | None = None) -> httpx.Response:
    headers = {"set-cookie": f"passport={passport}; Path=/; HttpOnly"} if passport else {}
    return httpx.Response(200, json={"code": 0, "data": data}, headers=headers)


def _user(account_id: str = "42", **extra) -> dict:
    return {"user_info": {"account_id": account_id, "email": "a@example.com", **extra}}


@asynccontextmanager
async def _provider(handler):
    """A provider wired to ``handler`` through the same jar-less client the
    real one uses, so the cookie-isolation property is the real one."""
    async with open_meeting_http(_ORIGIN, transport=httpx.MockTransport(handler)) as http:
        transport = MeetingTransport(
            http, app_id="2", cookie_name="passport", accept_language="zh-HK", timeout=5
        )
        yield MeetingIdentityProvider(
            MeetingAccounts(transport, login_public_key=_PUBLIC_PEM), issuer=_ORIGIN
        )


async def test_sign_in_identifies_the_account_by_what_meeting_reports() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/meeting/login":
            return _ok(passport="p1")
        return _ok(_user("42", nickname="Alice"))

    async with _provider(handler) as provider:
        grant = await provider.authenticate("a@example.com", "pw123456")

    assert grant.identity.subject == "42"
    assert grant.identity.issuer == _ORIGIN
    assert grant.identity.display_name == "Alice"
    assert grant.credential.passport == "p1"
    # The password never travels in the clear: Meeting gets the RSA
    # envelope, which only its private key opens.
    sent = json.loads(seen[0].content)
    assert sent["password"] != "pw123456"
    # The user-info call carries the session it was just given, and the
    # device id the session is bound to.
    assert seen[1].headers["cookie"] == "passport=p1"
    assert seen[1].headers["x-device-id"] == grant.credential.device_id


async def test_a_session_cookie_is_never_replayed_for_someone_else() -> None:
    # httpx remembers Set-Cookie by default. With one shared client that
    # would attach the last user's passport to the next user's request.
    cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookies.append(request.headers.get("cookie"))
        if request.url.path == "/meeting/login":
            return _ok(passport=f"p{len(cookies)}")
        return _ok(_user())

    async with _provider(handler) as provider:
        await provider.authenticate("a@example.com", "pw123456")
        await provider.send_verification_code(
            email="b@example.com", purpose="register", invite_code=None
        )

    assert cookies[-1] is None


async def test_a_rotated_passport_is_the_one_kept() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(_user(), passport="rotated")

    async with _provider(handler) as provider:
        grant = await provider.verify(MeetingSession("old", "d"), timeout=1)

    assert grant.credential == MeetingSession("rotated", "d")


async def test_a_cleared_cookie_is_not_a_new_passport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 0, "data": _user()},
            headers={"set-cookie": "passport=; Max-Age=0"},
        )

    async with _provider(handler) as provider:
        grant = await provider.verify(MeetingSession("old", "d"), timeout=1)

    assert grant.credential.passport == "old"


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("2001", InvalidCredentialsError),
        ("2004", AccountFrozenError),
        ("2006", AccountLockedError),
        # A code we cannot read is never guessed at as "wrong password".
        ("9999", IdentityProviderUnavailableError),
    ],
)
async def test_sign_in_refusals_map_to_what_the_user_should_do(code, error) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": code, "message": "no"})

    async with _provider(handler) as provider:
        with pytest.raises(error):
            await provider.authenticate("a@example.com", "pw123456")


async def test_a_password_too_long_to_encrypt_is_just_wrong() -> None:
    async with _provider(lambda _request: pytest.fail("must not call Meeting")) as provider:
        with pytest.raises(InvalidCredentialsError):
            await provider.authenticate("a@example.com", "x" * 1000)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401),
        httpx.Response(200, json={"code": "401"}),
        httpx.Response(200, json={"code": 2001}),
    ],
)
async def test_meeting_saying_signed_out_rejects_the_credential(response) -> None:
    async with _provider(lambda _request: response) as provider:
        with pytest.raises(CredentialRejected):
            await provider.verify(MeetingSession("p", "d"), timeout=1)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502),
        httpx.Response(429),
        httpx.Response(200, text="<html>gateway</html>"),
        httpx.Response(200, json={"code": "5000"}),
    ],
)
async def test_meeting_not_answering_is_not_a_sign_out(response) -> None:
    # Only Meeting *saying* a session is over may end ours; anything else
    # is an outage, and must not log everyone out.
    async with _provider(lambda _request: response) as provider:
        with pytest.raises(IdentityProviderUnavailableError):
            await provider.verify(MeetingSession("p", "d"), timeout=1)


@pytest.mark.parametrize(
    "data",
    [
        {"user_info": {"user_id": "42", "email": "a@example.com"}},
        {"account_id": "42", "email": "a@example.com"},
        {"user_info": {"account_id": ""}},
    ],
)
async def test_only_meetings_account_id_names_the_account(data) -> None:
    # The account id is the subject a local account is filed under. Another
    # id-shaped field, or the same one somewhere else in the payload, must
    # not be taken for it: that would sign the user in as a new account.
    async with _provider(lambda _request: _ok(data)) as provider:
        with pytest.raises(IdentityProviderUnavailableError):
            await provider.verify(MeetingSession("p", "d"), timeout=1)


async def test_a_transport_failure_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with _provider(handler) as provider:
        with pytest.raises(IdentityProviderUnavailableError):
            await provider.verify(MeetingSession("p", "d"), timeout=1)


async def test_account_request_refusals_carry_meetings_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "3002"})

    async with _provider(handler) as provider:
        with pytest.raises(IdentityProviderRejectedError) as caught:
            await provider.send_verification_code(
                email="a@example.com", purpose="register", invite_code=None
            )

    assert caught.value.details == {"code": "3002"}


async def test_account_requests_are_throttled_as_such() -> None:
    async with _provider(lambda _request: httpx.Response(429)) as provider:
        with pytest.raises(IdentityProviderRateLimitedError):
            await provider.reset_password(email="a@example.com", password="pw123456", code="123456")


@pytest.mark.parametrize("password", ["short", "x" * 16, "密密密密密密"])  # 18 bytes
async def test_new_passwords_meet_meetings_rules_before_it_is_asked(password) -> None:
    async with _provider(lambda _request: pytest.fail("must not call Meeting")) as provider:
        with pytest.raises(PasswordPolicyError):
            await provider.register(
                email="a@example.com", password=password, code="123456", name="A", invite_code=None
            )


async def test_a_name_over_meetings_bound_is_refused_before_it_is_asked() -> None:
    async with _provider(lambda _request: pytest.fail("must not call Meeting")) as provider:
        with pytest.raises(NamePolicyError) as caught:
            await provider.register(
                email="a@example.com",
                password="pw123456",
                code="123456",
                name="密" * 14,  # 42 bytes
                invite_code=None,
            )

    assert caught.value.details == {"max_bytes": 40}


async def test_registration_forwards_an_invitation_code() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _ok()

    async with _provider(handler) as provider:
        await provider.register(
            email="a@example.com",
            password="pw123456",
            code="123456",
            name="Alice",
            invite_code="INV",
        )

    assert bodies[0]["invitation_code"] == "INV"
    assert bodies[0]["verify_code"] == "123456"
