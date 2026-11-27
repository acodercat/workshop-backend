"""Meeting's account endpoints: sign-in, user info, sign-out, registration.

Reports what Meeting said; what it means for sign-in is
:mod:`app.services.identity.provider`'s to decide — which is why refusals
come back as Meeting's own codes rather than application errors.
"""

import base64
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from app.integrations.meeting.transport import (
    OK_CODES,
    MeetingActor,
    MeetingRateLimited,
    MeetingRefused,
    MeetingSession,
    MeetingTransport,
    MeetingUnavailable,
    envelope,
)


class PasswordTooLong(ValueError):
    """Too long to encrypt for Meeting's sign-in, so no account's password."""


@dataclass(frozen=True)
class MeetingAccount:
    """An account as ``get_user_info`` reports it."""

    account_id: str
    email: str | None
    display_name: str


def _first_text(source: dict[str, Any], *keys: str) -> str | None:
    """The first of ``keys`` holding a non-empty value, as text."""
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _account(data: Any) -> MeetingAccount:
    """Read an account out of a ``get_user_info`` payload.

    The account is ``data.user_info`` and its id is ``account_id`` — one
    field, read under one name. It is the subject every local account is
    filed under, so a payload without it is no answer at all: falling back
    to some other id-shaped field would sign the user in as a new account.

    The display name only describes the account, and Meeting has carried it
    under several keys, so that one is looked up under each of them.
    """
    user = data.get("user_info") if isinstance(data, dict) else None
    if not isinstance(user, dict):
        raise MeetingUnavailable("the answer carries no user info")
    account_id = _first_text(user, "account_id")
    if account_id is None:
        raise MeetingUnavailable("user info names no account")
    email = user.get("email")
    email = email if isinstance(email, str) and email else None
    display_name = (
        _first_text(user, "nickname", "nick_name", "screen_name", "name", "username")
        or email
        or account_id
    )
    return MeetingAccount(account_id=account_id, email=email, display_name=display_name)


class MeetingAccounts:
    def __init__(self, transport: MeetingTransport, *, login_public_key: str) -> None:
        self._transport = transport
        key = load_pem_public_key(login_public_key.encode())
        if not isinstance(key, RSAPublicKey):
            raise ValueError("the Meeting login key is not an RSA public key")
        self._login_key = key
        # PKCS#1 v1.5 spends 11 bytes of the modulus on padding; anything
        # longer cannot be encrypted at all.
        self._max_password_bytes = key.key_size // 8 - 11

    def _wrap_password(self, password: str) -> str:
        """Encrypt a password the way Meeting's own sign-in page does."""
        data = password.encode()
        if len(data) > self._max_password_bytes:
            raise PasswordTooLong
        return base64.b64encode(self._login_key.encrypt(data, padding.PKCS1v15())).decode()

    async def login(self, account: str, password: str) -> MeetingSession:
        """Open a session with an account's own credentials.

        Raises :class:`MeetingRefused` with Meeting's code when it declines
        (wrong password, frozen, locked, …), and :class:`PasswordTooLong`
        before asking when the password cannot be encrypted.
        """
        actor = MeetingActor(MeetingSession.anonymous())
        response = await self._transport.call(
            "POST",
            "/meeting/login",
            actor,
            body={"account": account, "password": self._wrap_password(password)},
            app_query=False,
        )
        # Read by hand rather than through ``envelope``: on this one call the
        # envelope code is the answer even when the HTTP status is an error,
        # and ``2001`` means "wrong password", not "not signed in".
        if response.status_code >= 500:
            raise MeetingUnavailable(f"HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        code = str(payload.get("code", "")) if isinstance(payload, dict) else ""
        if not (response.is_success and code in OK_CODES):
            if code and code not in OK_CODES:
                raise MeetingRefused(code)
            if response.status_code == 429:
                raise MeetingRateLimited
            raise MeetingUnavailable(f"HTTP {response.status_code} without a code")
        if not actor.session.passport:
            raise MeetingUnavailable("sign-in succeeded without a session cookie")
        return actor.session

    async def user_info(
        self, actor: MeetingActor, *, timeout: float | None = None
    ) -> MeetingAccount:
        """Who ``actor`` is signed in as. :class:`~.transport.MeetingDenied` if nobody."""
        response = await self._transport.call(
            "POST",
            "/meeting/api/v1/get_user_info",
            actor,
            body={},
            app_query=False,
            timeout=timeout,
        )
        return _account(envelope(response))

    async def logout(self, actor: MeetingActor) -> None:
        await self._transport.data("POST", "/meeting/api/v1/logout", actor, body={})

    async def send_verify_code(
        self, *, email: str, recover: bool, invitation_code: str | None
    ) -> None:
        body: dict[str, Any] = {"email": email, "type": 2 if recover else 1}
        if invitation_code:
            body["invitation_code"] = invitation_code
        await self._anonymous("/meeting/auth/send_verify_code", body)

    async def register(
        self, *, email: str, password: str, code: str, name: str, invitation_code: str | None
    ) -> None:
        body: dict[str, Any] = {
            "email": email,
            "password": self._wrap_password(password),
            "verify_code": code,
            "name": name,
        }
        if invitation_code:
            body["invitation_code"] = invitation_code
        await self._anonymous("/meeting/auth/register", body)

    async def forget_password(self, *, email: str, password: str, code: str) -> None:
        await self._anonymous(
            "/meeting/auth/forget_password",
            {"email": email, "password": self._wrap_password(password), "verify_code": code},
        )

    async def _anonymous(self, path: str, body: dict[str, Any]) -> None:
        response = await self._transport.call(
            "POST", path, MeetingActor(MeetingSession.anonymous()), body=body
        )
        envelope(response, signed_in=False)
