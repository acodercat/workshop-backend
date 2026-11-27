"""How a request to Meeting is made, and how its answer is read.

Three properties of the protocol shape everything here:

**The session is a cookie, and it rotates.** Meeting may answer any call
with a fresh ``Set-Cookie`` for the session. A call is therefore made *as*
a :class:`MeetingActor`, whose session is replaced in place when Meeting
rotates it, and whose ``on_rotate`` hook lets the owner persist the new
value before anything else uses the old one.

**Sessions must never share a jar.** One ``httpx`` client serves every
user. Left to itself it would remember each response's cookie and attach
it to the next request for the same host — the next request being some
*other* user's. The client is built with a jar that accepts nothing
(:func:`open_meeting_http`), and the cookie is set per request by hand.

**Two layers of status.** A call can fail as HTTP, or succeed as HTTP and
carry a refusal in the envelope's ``code``. :func:`envelope` reads both.
"""

import http.cookiejar
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

# Envelope codes meaning "done". Meeting sends either, as a number or a
# string depending on the endpoint, so both are compared as text.
OK_CODES = frozenset({"0", "200"})
# Envelope codes meaning "this session is not signed in", on calls made
# with one. ``2001`` doubles as "wrong password" on the sign-in call, which
# reads the envelope itself for that reason.
DENIED_CODES = frozenset({"401", "403", "2001"})

# Meeting identifies the kind of client by this header; ``0`` is the web
# client, the only kind whose API this backend has been shown.
_DEVICE_SOURCE = "0"


class MeetingError(Exception):
    """Base for everything this package raises."""


class MeetingUnavailable(MeetingError):
    """No usable answer: a transport failure, a 5xx, or a body that is not
    Meeting's envelope. Says nothing about the request's merits."""


class MeetingDenied(MeetingError):
    """Meeting does not consider the session that was sent signed in."""


class MeetingRateLimited(MeetingError):
    """Meeting is throttling this caller (HTTP 429)."""


class MeetingRefused(MeetingError):
    """Meeting understood the request and declined it, with a code of its own."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MeetingSession:
    """What it takes to make a call as one signed-in Meeting account.

    ``device_id`` is half of it: Meeting ties a session to the device that
    opened it, so a passport presented with a different id is refused. It
    is minted once at sign-in and kept with the passport for the session's
    whole life.
    """

    passport: str
    device_id: str

    @classmethod
    def anonymous(cls) -> "MeetingSession":
        """For calls nobody is signed in for. Meeting still wants a device
        id; a throwaway one, since no session will ever be tied to it."""
        return cls(passport="", device_id=str(uuid.uuid4()))


RotationHook = Callable[[MeetingSession, MeetingSession], Awaitable[None]]


@dataclass
class MeetingActor:
    """The account a sequence of calls is made as.

    Mutable on purpose: when Meeting rotates the session mid-sequence, every
    later call in the sequence must present the new one. ``on_rotate`` is
    awaited with ``(old, new)`` before the call that rotated returns.
    """

    session: MeetingSession
    on_rotate: RotationHook | None = field(default=None, repr=False)


def envelope(response: httpx.Response, *, signed_in: bool = True) -> Any:
    """Unwrap ``{"code": ..., "data": ...}`` or raise what the response is.

    HTTP status first, then the envelope: a 200 is no promise, since
    Meeting reports most refusals inside one.

    ``signed_in`` says whether the call carried a session. Only then can a
    401 / 403 / ``2001`` mean "this session is not signed in"; on a call
    made before anyone is (registration, a code, a password reset) the same
    answers are refusals of the request, and are raised as such.
    """
    if not signed_in and response.status_code in (401, 403):
        raise MeetingRefused(str(response.status_code))
    raise_for_http(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise MeetingUnavailable("response is not JSON") from exc
    if not isinstance(payload, dict) or "code" not in payload:
        raise MeetingUnavailable("response is not an envelope")
    code = str(payload["code"])
    if code in DENIED_CODES and signed_in:
        raise MeetingDenied
    if code not in OK_CODES:
        raise MeetingRefused(code)
    return payload.get("data")


def raise_for_http(response: httpx.Response) -> None:
    if response.status_code == 429:
        raise MeetingRateLimited
    if response.status_code in (401, 403):
        raise MeetingDenied
    if not response.is_success:
        raise MeetingUnavailable(f"HTTP {response.status_code}")


class MeetingTransport:
    """Makes calls to Meeting as a given actor. Stateless across actors:
    one instance serves every user."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        app_id: str,
        cookie_name: str,
        accept_language: str,
        timeout: float,
    ) -> None:
        self._http = http
        self._app_id = app_id
        self._cookie_name = cookie_name
        self._accept_language = accept_language
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        actor: MeetingActor,
        *,
        body: dict[str, Any] | None,
        params: dict[str, str] | None,
        app_query: bool,
        timeout: float | None,
    ) -> httpx.Request:
        # Every Meeting API lives under ``/meeting/``. Holding paths to that
        # is what keeps a caller-supplied fragment from steering a request
        # that carries a session cookie somewhere else on the host.
        if not path.startswith("/meeting/"):
            raise ValueError(f"not a Meeting API path: {path!r}")
        headers = {
            "x-app-id": self._app_id,
            "x-device-id": actor.session.device_id,
            "x-device-source": _DEVICE_SOURCE,
            "x-acc-lang": self._accept_language,
        }
        if actor.session.passport:
            headers["cookie"] = f"{self._cookie_name}={actor.session.passport}"
        query = dict(params or {})
        # Meeting wants the app id in the query on its API routes and not on
        # the sign-in routes; the header goes on all of them.
        if app_query:
            query["app_id"] = self._app_id
        seconds = self.timeout if timeout is None else timeout
        return self._http.build_request(
            method,
            path,
            json=body if method != "GET" else None,
            params=query or None,
            headers=headers,
            timeout=httpx.Timeout(seconds),
        )

    async def call(
        self,
        method: str,
        path: str,
        actor: MeetingActor,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        app_query: bool = True,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Send one request as ``actor`` and return the raw response."""
        request = self._request(
            method, path, actor, body=body, params=params, app_query=app_query, timeout=timeout
        )
        try:
            response = await self._http.send(request)
        except httpx.HTTPError as exc:
            raise MeetingUnavailable(type(exc).__name__) from exc
        await self._absorb_rotation(response, actor)
        return response

    async def data(
        self,
        method: str,
        path: str,
        actor: MeetingActor,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send one request and return the envelope's ``data``."""
        return envelope(
            await self.call(method, path, actor, body=body, params=params, timeout=timeout)
        )

    @asynccontextmanager
    async def stream(
        self,
        path: str,
        actor: MeetingActor,
        *,
        body: dict[str, Any],
        timeout: float,
    ) -> AsyncIterator[httpx.Response]:
        """POST and yield the response with its body unread, for the
        endpoints that stream their answer (see :mod:`.output`)."""
        request = self._request(
            "POST", path, actor, body=body, params=None, app_query=True, timeout=timeout
        )
        try:
            response = await self._http.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise MeetingUnavailable(type(exc).__name__) from exc
        try:
            await self._absorb_rotation(response, actor)
            yield response
        finally:
            await response.aclose()

    async def _absorb_rotation(self, response: httpx.Response, actor: MeetingActor) -> None:
        prefix = f"{self._cookie_name}="
        for header in response.headers.get_list("set-cookie"):
            pair = header.split(";", 1)[0].strip()
            # An empty value is a cookie being cleared, not a new session.
            if not pair.startswith(prefix) or len(pair) == len(prefix):
                continue
            passport = pair[len(prefix) :]
            if passport == actor.session.passport:
                return
            old = actor.session
            actor.session = MeetingSession(passport=passport, device_id=old.device_id)
            if actor.on_rotate is not None:
                await actor.on_rotate(old, actor.session)
            return


@asynccontextmanager
async def open_meeting_http(
    origin: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """The one ``httpx`` client all Meeting calls share.

    Built here, not inline at the call site, because two of its settings
    are safety properties of this integration rather than preferences:

    - **A cookie jar that accepts nothing.** See the module docstring —
      without it one user's session cookie rides along on the next user's
      request. ``allowed_domains=[]`` is the standard library's own way to
      say "no domain may set a cookie".
    - **No redirects.** A password or a session cookie is in every request;
      following a redirect would carry it wherever Meeting pointed.

    ``transport`` exists for tests, which drive the real client against a
    fake Meeting rather than a hand-built one with different settings.
    """
    jar = http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
    async with httpx.AsyncClient(
        base_url=origin, cookies=jar, follow_redirects=False, transport=transport
    ) as client:
        yield client
