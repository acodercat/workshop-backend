"""The Meeting service as this deployment's identity provider.

:mod:`app.integrations.meeting` reports what Meeting said; this module says
what it means for sign-in — mostly a translation table from Meeting's
refusal codes onto the application's typed errors, plus Meeting's bounds on
a new password, checked here first because Meeting's own answer to a bad
one is a bare code nobody can act on.

The credential a sign-in yields is Meeting's session
(:class:`~app.integrations.meeting.transport.MeetingSession`). It is
sealed into the ``identity_session`` row (:mod:`.credentials`) and used by
the meeting workspace to act as the user (:mod:`app.services.meeting.delegation`).
"""

import logging
from dataclasses import dataclass
from typing import Literal

from app.errors import (
    AccountFrozenError,
    AccountInactiveError,
    AccountLockedError,
    AppError,
    IdentityProviderRateLimitedError,
    IdentityProviderRejectedError,
    IdentityProviderUnavailableError,
    InvalidCredentialsError,
    PasswordPolicyError,
)
from app.integrations.meeting.accounts import MeetingAccount, MeetingAccounts, PasswordTooLong
from app.integrations.meeting.transport import (
    MeetingActor,
    MeetingDenied,
    MeetingError,
    MeetingRateLimited,
    MeetingRefused,
    MeetingSession,
)

logger = logging.getLogger(__name__)

# Stored as ``user_identity.provider``.
PROVIDER_NAME = "meeting"

CodePurpose = Literal["register", "recover"]

# Meeting's bounds on a password it will store, in bytes...
MIN_PASSWORD_BYTES = 8
MAX_PASSWORD_BYTES = 15
# ...and on a display name.
MAX_NAME_BYTES = 40

# What each sign-in refusal code means. A code missing from here is not a
# refusal we understand, and is reported as the provider being unavailable
# rather than guessed at — see :func:`_sign_in_error`.
_SIGN_IN_REFUSALS: dict[str, type[AppError]] = {
    "2001": InvalidCredentialsError,
    "2004": AccountFrozenError,
    "2006": AccountLockedError,
    "2008": AccountInactiveError,
}


@dataclass(frozen=True)
class ExternalIdentity:
    """An account as the provider describes it.

    ``(provider, issuer, subject)`` is the identity; ``email`` and
    ``display_name`` describe it, may change at any time, and must never be
    used to find an account.
    """

    provider: str
    issuer: str
    subject: str
    email: str | None
    display_name: str


@dataclass(frozen=True)
class ProviderGrant:
    """An identity and the Meeting session currently valid for it.

    Returned by :meth:`MeetingIdentityProvider.verify` as well as by sign-in,
    because Meeting may rotate the session on any call — the one that comes
    back is the one to keep, whether or not it changed.
    """

    identity: ExternalIdentity
    credential: MeetingSession


class CredentialRejected(Exception):
    """Meeting no longer honours a stored session.

    Not an ``AppError``: it is not an answer for a client but a fact for the
    session layer, which revokes the session and refuses the request as
    unauthenticated. Letting it reach a client as Meeting's own 401 would
    describe our session in Meeting's terms.
    """


def _unavailable() -> IdentityProviderUnavailableError:
    return IdentityProviderUnavailableError("The identity provider is unavailable.")


def _sign_in_error(exc: MeetingError) -> AppError:
    if isinstance(exc, MeetingRateLimited):
        return IdentityProviderRateLimitedError("Too many sign-in attempts.")
    if isinstance(exc, MeetingRefused):
        refusal = _SIGN_IN_REFUSALS.get(exc.code)
        if refusal is not None:
            return refusal("Sign-in refused by the identity provider.")
        # An unmapped code is logged, never shown: telling a user "wrong
        # password" for a reason we cannot read sends them to reset a
        # password that was fine.
        logger.warning("meeting sign-in refused with unmapped code %s", exc.code)
    return _unavailable()


def _account_request_error(exc: MeetingError) -> AppError:
    """For the calls made before anyone is signed in: codes, registration,
    password reset."""
    if isinstance(exc, MeetingRateLimited):
        return IdentityProviderRateLimitedError("Too many requests to the identity provider.")
    if isinstance(exc, MeetingRefused):
        return IdentityProviderRejectedError(
            "The identity provider declined the request.", details={"code": exc.code}
        )
    return _unavailable()


def _check_new_password(password: str) -> None:
    if not MIN_PASSWORD_BYTES <= len(password.encode()) <= MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(
            f"Password must be {MIN_PASSWORD_BYTES}-{MAX_PASSWORD_BYTES} bytes.",
            details={"min_bytes": MIN_PASSWORD_BYTES, "max_bytes": MAX_PASSWORD_BYTES},
        )


class MeetingIdentityProvider:
    """Sign-in, re-confirmation, sign-out and account requests at Meeting."""

    name = PROVIDER_NAME

    def __init__(self, accounts: MeetingAccounts, *, issuer: str) -> None:
        self._accounts = accounts
        # The deployment vouching — stored as ``user_identity.issuer``.
        self.issuer = issuer

    def _grant(self, account: MeetingAccount, session: MeetingSession) -> ProviderGrant:
        return ProviderGrant(
            identity=ExternalIdentity(
                provider=self.name,
                issuer=self.issuer,
                subject=account.account_id,
                email=account.email,
                display_name=account.display_name,
            ),
            credential=session,
        )

    async def authenticate(self, account: str, password: str) -> ProviderGrant:
        """Sign in with the account's own credentials.

        Raises :class:`~app.errors.InvalidCredentialsError` (wrong password
        or unknown account, indistinguishably), one of the ``Account*``
        errors when Meeting says why the account may not sign in, or
        :class:`~app.errors.IdentityProviderUnavailableError`.
        """
        try:
            actor = MeetingActor(await self._accounts.login(account, password))
            # Sign-in returns a session, not an identity. Asking who it
            # belongs to is what turns "these credentials work" into "this
            # is account N" — the subject is never taken from what the
            # user typed.
            profile = await self._accounts.user_info(actor)
        except PasswordTooLong as exc:
            # No account's password: answered like any other wrong one.
            raise InvalidCredentialsError("Sign-in refused by the identity provider.") from exc
        except MeetingDenied as exc:
            # The session Meeting issued a moment ago is already refused:
            # its fault, not the user's.
            raise _unavailable() from exc
        except MeetingError as exc:
            raise _sign_in_error(exc) from exc
        return self._grant(profile, actor.session)

    async def verify(self, credential: MeetingSession, *, timeout: float) -> ProviderGrant:
        """Ask whether a stored session still stands, and for whom.

        Raises :class:`CredentialRejected` when Meeting says it does not, and
        :class:`~app.errors.IdentityProviderUnavailableError` when Meeting
        did not say. Only the first may end the session.
        """
        actor = MeetingActor(credential)
        try:
            profile = await self._accounts.user_info(actor, timeout=timeout)
        except MeetingDenied as exc:
            raise CredentialRejected from exc
        except MeetingError as exc:
            # Throttled, refused with a code, unreachable: none of these is
            # Meeting saying the session is over, so none may end it.
            raise _unavailable() from exc
        return self._grant(profile, actor.session)

    async def revoke(self, credential: MeetingSession) -> None:
        """End the session at Meeting. Best-effort: the local session is
        already gone when this is called."""
        try:
            await self._accounts.logout(MeetingActor(credential))
        except MeetingDenied:
            return  # already signed out there — the state we wanted
        except MeetingError as exc:
            raise _unavailable() from exc

    async def send_verification_code(
        self, *, email: str, purpose: CodePurpose, invite_code: str | None
    ) -> None:
        try:
            await self._accounts.send_verify_code(
                email=email, recover=purpose == "recover", invitation_code=invite_code
            )
        except MeetingError as exc:
            raise _account_request_error(exc) from exc

    async def register(
        self, *, email: str, password: str, code: str, name: str, invite_code: str | None
    ) -> None:
        """Create the account at Meeting. Signs nobody in — the caller
        follows with :meth:`authenticate`, so a new account and a returning
        one enter through the same door."""
        _check_new_password(password)
        if len(name.encode()) > MAX_NAME_BYTES:
            raise IdentityProviderRejectedError(
                f"Name must be at most {MAX_NAME_BYTES} bytes.", details={"code": "name_too_long"}
            )
        try:
            await self._accounts.register(
                email=email, password=password, code=code, name=name, invitation_code=invite_code
            )
        except MeetingError as exc:
            raise _account_request_error(exc) from exc

    async def reset_password(self, *, email: str, password: str, code: str) -> None:
        _check_new_password(password)
        try:
            await self._accounts.forget_password(email=email, password=password, code=code)
        except MeetingError as exc:
            raise _account_request_error(exc) from exc
