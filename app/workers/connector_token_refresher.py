"""Keep OAuth Connectors' access tokens fresh, from one place.

A refresh token is single-use: presenting one another party has already spent
is what authorization servers answer by revoking the whole grant family, and
the user is sent back to the browser. So exactly one thing may rotate a given
grant — and this is it.

That "exactly one" is the whole reason this job exists here rather than in the
worker. The worker runs N replicas with no session affinity, and its critical
section is an entire agent turn, far too long to hold a database lock across;
approximating one there took a lease column, fencing tokens, CAS writes and a
peer-wait loop. Here the critical section is a single token round-trip, so
``SELECT … FOR UPDATE SKIP LOCKED`` — the same idiom the turn queue claims with
— serialises every replica of this process and needs nothing else. It also
cannot strand: a killed process drops its connection and Postgres releases the
lock, which is what the lease's arithmetic was trying to guarantee by hand.

The worker therefore never touches an OAuth token. It reads whatever access
token is on the row and sends it as a bearer credential, exactly as it does for
a static one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata
from sqlalchemy import or_, select

from app.database import AsyncSessionLocal
from app.models.mcp_server import MCPServer, MCPServerAuthType
from app.services.crypto import decrypt_secret
from app.services.mcp import grant, oauth
from config import settings

logger = logging.getLogger(__name__)

# How long to leave a Connector alone after a rotation *failed* for a reason
# that might pass. A transient failure changes nothing about the row, and the
# due query sorts oldest-expiry-first — so without this the same handful of
# unwell Connectors is selected every tick, forever, and everything behind them
# is never refreshed at all. It only takes a provider that disappears (which
# answers "unreachable", not ``invalid_grant``, so it is never flagged) for that
# set to accumulate on its own.
_RETRY_AFTER_SECONDS = 300

# Ceiling on the second attempt at writing a rotated grant. It runs after
# something already went wrong, so it gets a bound of its own rather than
# inheriting the confidence of the first.
_SECOND_ATTEMPT_TIMEOUT = 20.0


class _CancelledMidRotation(Exception):
    """A cancellation that arrived while a rotated grant was being written.

    Carries the rotation so the caller can land it before letting the
    cancellation continue. An ordinary ``Exception`` on purpose: a
    ``BaseException`` would pass straight through the handler that has to do
    the landing, which is the whole reason this exists.
    """

    def __init__(self, rotation: _Rotation) -> None:
        super().__init__("cancelled while writing a rotated grant")
        self.rotation = rotation


@dataclass(frozen=True)
class _Rotation:
    """A rotated grant whose first write did not land, as plain values.

    Detached from the ORM on purpose. It is carried out of a session whose
    commit just failed, and touching an attribute on an object still attached
    to that session can raise on its own.
    """

    connector_id: int
    ciphertext: str
    expires_at: datetime | None
    refresh_after: datetime | None
    spent: str | None


async def run_forever(*, interval_seconds: int | None = None) -> None:
    """Periodic refresh loop. Cancel-safe via :class:`asyncio.CancelledError`."""
    interval = interval_seconds or settings.connector_refresh_interval_seconds
    consecutive_failures = 0
    while True:
        try:
            refreshed = await _tick()
            consecutive_failures = 0
            if refreshed:
                logger.info("connector refresher tick: refreshed=%d", refreshed)
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            logger.exception(
                "connector refresher tick failed (consecutive_failures=%d)",
                consecutive_failures,
            )
        # Exponential backoff (capped at 10 min), as in the settle sweeper:
        # protects the database when something is fundamentally broken without
        # delaying recovery past the point of usefulness.
        delay = (
            min(interval * (2 ** consecutive_failures), 600)
            if consecutive_failures
            else interval
        )
        await asyncio.sleep(delay)


async def _tick() -> int:
    """One pass. Returns how many grants were actually rotated.

    No tick-wide lock, unlike the settle sweeper: the contention here is
    per-Connector, and every replica taking a global lock would serialise
    unrelated rows behind whichever one has a slow authorization server. Each
    row is claimed and rotated in its own short transaction instead, so two
    replicas simply split the batch.
    """
    async with AsyncSessionLocal() as db:
        due = await _list_due(db)
    refreshed = 0
    for connector_id in due:
        if await _refresh_one(connector_id):
            refreshed += 1
    return refreshed


async def _list_due(db) -> list[int]:
    """Ids whose access token expires within the look-ahead window.

    Ids rather than rows: the transaction that reads this one is not the
    transaction that rotates, so anything else read here would be stale by the
    time it is used — :func:`_refresh_one` re-reads under its own lock.

    Rows already asking for re-authorization are left out. Their refresh token
    is known dead, so retrying it every tick would only spend requests to be
    refused, and the flag is cleared by the authorize flow that fixes it.

    So are the ones whose ``oauth_refresh_after`` has not come round — and in
    the query rather than after it. Filtering a batch that has already been
    ``LIMIT``-ed just leaves the batch short: those rows are the oldest-expiry
    ones, so they would occupy every slot and nothing behind them would be
    selected at all.

    Two conditions rather than one because expiry alone answers the wrong
    question for a short-lived token. A grant that lives less than
    ``connector_refresh_ahead_seconds`` is inside the look-ahead the instant it
    is written, so on expiry alone it is due again on the very next tick and
    sorts ahead of every healthy row precisely because it expires soonest.
    ``oauth_refresh_after`` is the answer to "when should we act", written from
    what the last attempt actually learned, and it is on the row so every
    replica sees it.
    """
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(seconds=settings.connector_refresh_ahead_seconds)
    query = select(MCPServer.id).where(
        MCPServer.auth_type == MCPServerAuthType.OAUTH,
        MCPServer.oauth_tokens_encrypted.is_not(None),
        MCPServer.oauth_tokens_expires_at.is_not(None),
        MCPServer.oauth_tokens_expires_at < horizon,
        MCPServer.oauth_reauthorization_required.is_(False),
        or_(
            MCPServer.oauth_refresh_after.is_(None),
            MCPServer.oauth_refresh_after <= now,
        ),
    )
    rows = await db.execute(
        query.order_by(MCPServer.oauth_tokens_expires_at.asc())
        .limit(settings.connector_refresh_batch)
    )
    return [row[0] for row in rows.all()]


def _next_attempt_after(expires_at: datetime | None) -> datetime:
    """When to come back to a grant that expires at *expires_at*.

    Two answers, and the later one wins.

    The first is when :func:`_list_due` would pick the row up again on expiry
    alone — ``expires_at`` less the look-ahead. For a token that outlives the
    window that is the whole answer, and this changes nothing about today's
    behaviour.

    The second is what makes a *short-lived* token work. Its new expiry is
    already inside the look-ahead, so the first answer is in the past and would
    mean "next tick, and every tick after" — a provider refreshed once a minute
    for a token it will happily reissue at half the age, while the row sits
    ahead of every healthy one in the ordering. Half of the token's own life is
    the answer that scales with what the server actually gave us: it leaves the
    same proportion of margin whatever the lifetime, and for a 300-second token
    means one refresh every 150 seconds rather than five.
    """
    now = datetime.now(timezone.utc)
    if expires_at is None:
        return now
    due_on_expiry = expires_at - timedelta(
        seconds=settings.connector_refresh_ahead_seconds
    )
    half_life = now + (expires_at - now) / 2
    return max(due_on_expiry, half_life)


async def _refresh_one(connector_id: int) -> bool:
    """Rotate one Connector's grant; never raises.

    Two steps rather than one, and the seam matters: :func:`_rotate` owns the
    transaction that holds the row lock, and anything that has to be retried
    happens *after* that transaction has closed. Retrying inside it would take
    a second lock on the same row while the first session still holds it — a
    wait nobody can end, since the holder is waiting for the retry to return.
    """
    try:
        rotated, pending = await _rotate(connector_id)
    except _CancelledMidRotation as interrupted:
        # The session that held the row is closed by now — the exception
        # unwound its ``async with`` — so this second attempt cannot deadlock
        # against it, the same seam the ordinary retry below relies on.
        # Shielded so the cancellation that got us here does not also take the
        # write, and bounded so a database going down with the process cannot
        # hold up the exit.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                asyncio.shield(_commit_again(interrupted.rotation)),
                _SECOND_ATTEMPT_TIMEOUT,
            )
        raise asyncio.CancelledError from None
    except Exception:
        # One unwell Connector must not end the tick for the rest of the batch.
        # When to try it again was already decided under the lock, by whichever
        # step failed; nothing out here knows enough to overrule that.
        logger.warning(
            "Could not refresh Connector %s; leaving it for a later tick",
            connector_id, exc_info=True,
        )
        return False
    if pending is not None:
        await _commit_again(pending)
    return rotated


async def _rotate(connector_id: int) -> tuple[bool, _Rotation | None]:
    """Claim one Connector, rotate its grant, and try to write the result.

    Returns whether anything was rotated, and — only when the write itself
    failed — what the caller must land once this transaction is out of the way.

    The lock is taken with ``SKIP LOCKED`` so a replica that finds the row
    already claimed moves on rather than queueing behind a token endpoint.

    Claim, round-trip and write are one transaction on purpose: it is what
    makes the new refresh token durable before the lock is released, so no peer
    can read the spent one afterwards. The round-trip is bounded by the OAuth
    service's own deadline, which is what keeps a hanging provider from holding
    the row.
    """
    async with AsyncSessionLocal() as db:
        connector = await db.scalar(
            select(MCPServer)
            .where(MCPServer.id == connector_id)
            .with_for_update(skip_locked=True)
        )
        if connector is None:
            return False, None  # deleted, or another replica has it
        if not _still_due(connector):
            return False, None  # a peer rotated it while this batch queued
        authorization = _stored_authorization(connector)
        if authorization is None:
            return False, None
        metadata, client = authorization
        tokens = grant.stored_tokens(connector)
        if tokens is None or not tokens.refresh_token:
            _refuse(connector, "no refresh token was ever issued")
            await db.commit()
            return False, None
        try:
            rotated = await oauth.refresh_tokens(
                metadata, client,
                refresh_token=tokens.refresh_token,
                server_url=connector.url,
            )
        except oauth.GrantRevoked:
            _refuse(connector, "the authorization server rejected the grant")
            await db.commit()
            return False, None
        except Exception:
            # Recorded here, still holding the lock, rather than from a second
            # transaction afterwards. A backoff written later has no way to
            # tell whether a peer has since rotated this row and scheduled it
            # properly; under the lock the question cannot arise, because no
            # peer could have touched it. Whatever went wrong is the caller's
            # to log — this only decides when to come back.
            connector.oauth_refresh_after = datetime.now(timezone.utc) + timedelta(
                seconds=_RETRY_AFTER_SECONDS
            )
            await db.commit()
            raise
        spent = connector.oauth_tokens_encrypted
        grant.store_tokens(connector, rotated, rotation=True)
        connector.oauth_refresh_after = _next_attempt_after(
            connector.oauth_tokens_expires_at
        )
        pending = _Rotation(
            connector_id=connector.id,
            ciphertext=connector.oauth_tokens_encrypted,
            expires_at=connector.oauth_tokens_expires_at,
            refresh_after=connector.oauth_refresh_after,
            spent=spent,
        )
        try:
            await db.commit()
        except asyncio.CancelledError:
            # Converted, not propagated — for the length of one write. By now
            # the provider has spent the old refresh token, so the replacement
            # in *pending* is the only copy in existence and dying with it
            # costs the user a trip through the browser. The caller lands it
            # and then re-raises, so the cancellation still wins; see
            # :func:`_refresh_one`.
            raise _CancelledMidRotation(pending) from None
        except Exception:
            # Release the row before anyone tries to take it again, and hand
            # the caller plain values — this session is no longer safe to read
            # attributes through.
            await db.rollback()
            return True, pending
        return True, None


async def _commit_again(pending: _Rotation) -> None:
    """Land a rotation whose first write failed, in a fresh transaction.

    The one write here that cannot simply be retried later: by the time it runs
    the provider has already consumed the old refresh token, so the row holds a
    spent one and its replacement exists only in memory. Lose it and the next
    tick asks with the spent token, is told ``invalid_grant``, and the user is
    sent to the browser — out of a pool blip.

    Reached from two directions, and cancellation is one of them.
    ``asyncio.run`` installs its own SIGINT handler and cancels the main task,
    and a sibling in the ``TaskGroup`` failing fatally cancels this one too — so
    "the process is going away" is a live path, not a hypothetical, and
    ``CancelledError`` being a ``BaseException`` means it would otherwise sail
    past every handler between here and the top. :class:`_CancelledMidRotation`
    is how it is caught for exactly as long as this write takes.

    Fenced on the grant it replaces, and idempotent against the ambiguous case:
    a first commit that reached the database but lost the connection before
    saying so leaves the row already holding this exact ciphertext, which is
    read as success rather than written over. Anything else means someone moved
    the row since — a re-authorization, most likely — and theirs is newer.

    Bounded, because it runs after a failure and must not become the new hang.
    """
    try:
        async with asyncio.timeout(_SECOND_ATTEMPT_TIMEOUT):
            async with AsyncSessionLocal() as db:
                current = await db.scalar(
                    select(MCPServer)
                    .where(MCPServer.id == pending.connector_id)
                    .with_for_update()
                )
                if current is None or current.oauth_tokens_encrypted == (
                    pending.ciphertext
                ):
                    return
                if current.oauth_tokens_encrypted != pending.spent:
                    logger.warning(
                        "Connector %s moved on while its rotation was being "
                        "written; dropping ours", pending.connector_id,
                    )
                    return
                current.oauth_tokens_encrypted = pending.ciphertext
                current.oauth_tokens_expires_at = pending.expires_at
                current.oauth_refresh_after = pending.refresh_after
                await db.commit()
                logger.info(
                    "Connector %s: rotation landed on the second attempt",
                    pending.connector_id,
                )
    except Exception:
        logger.exception(
            "Connector %s: its rotated grant could not be written at all; the "
            "next authorization will have to replace it", pending.connector_id,
        )


def _still_due(connector: MCPServer) -> bool:
    """Whether the row read under the lock still needs what the batch selected.

    A batch is processed serially, so by the time a late row is reached another
    replica may have rotated it — refreshing again would spend a perfectly good
    refresh token for nothing.

    Both halves of :func:`_list_due`'s question, not just the expiry one. The
    expiry alone cannot see a peer's work on exactly the rows where seeing it
    matters: a short-lived token is inside the look-ahead the moment it is
    written, so a replica that took the lock a second after someone rotated it
    would find it "still due" and rotate it straight again.
    """
    if connector.oauth_tokens_expires_at is None:
        return False
    now = datetime.now(timezone.utc)
    if (
        connector.oauth_refresh_after is not None
        and connector.oauth_refresh_after > now
    ):
        return False
    horizon = now + timedelta(seconds=settings.connector_refresh_ahead_seconds)
    return connector.oauth_tokens_expires_at < horizon


def _stored_authorization(
    connector: MCPServer,
) -> tuple[OAuthMetadata, OAuthClientInformationFull] | None:
    """Where to send the refresh, and who to send it as.

    Both were captured when the user authorized. ``None`` when either is
    missing, which leaves the row alone: guessing a token endpoint would post
    the refresh token — and the client secret under it — to whatever host the
    Connector points at.
    """
    stored = connector.oauth_metadata or {}
    authorization_server = stored.get("authorization_server")
    if not authorization_server or not connector.oauth_client_encrypted:
        logger.warning(
            "Connector %s has no stored authorization metadata; not refreshing",
            connector.id,
        )
        return None
    return (
        OAuthMetadata.model_validate(authorization_server),
        OAuthClientInformationFull.model_validate_json(
            decrypt_secret(connector.oauth_client_encrypted)
        ),
    )


def _refuse(connector: MCPServer, why: str) -> None:
    """Mark a Connector as needing the user's browser again.

    The one state that asks something of the user, so it is set only on proof
    that no amount of retrying will help — never on a provider that is merely
    unreachable.
    """
    logger.info("Connector %s needs re-authorization: %s", connector.id, why)
    connector.oauth_reauthorization_required = True
