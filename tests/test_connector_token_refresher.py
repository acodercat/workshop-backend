"""Tests for the Connector token refresher — the only thing that may rotate.

A refresh token is single-use, so what these pin is mostly restraint: rotate
once, rotate nothing that a peer already rotated, and ask the user for a browser
only on proof that no retry will help.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from app.models.mcp_server import MCPServer, MCPServerAuthType
from app.services.mcp import oauth
from app.workers import connector_token_refresher as refresher

_AUTHORIZATION_SERVER = {
    "issuer": "https://as.example.com",
    "authorization_endpoint": "https://as.example.com/authorize",
    "token_endpoint": "https://as.example.com/token",
    "response_types_supported": ["code"],
}


def _connector(**over) -> MCPServer:
    base = dict(
        id=1, user_id=7, name="c", url="https://mcp.example.com/mcp",
        auth_type=MCPServerAuthType.OAUTH,
        oauth_client_encrypted="sealed-client",
        oauth_tokens_encrypted="sealed-tokens",
        oauth_metadata={"authorization_server": _AUTHORIZATION_SERVER},
        oauth_tokens_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        oauth_reauthorization_required=False,
    )
    base.update(over)
    return MCPServer(**base)


def _session_yielding(connector: MCPServer | None):
    """Stand in for ``AsyncSessionLocal()`` — one session, one locked row."""
    db = AsyncMock()
    db.scalar.return_value = connector
    db.__aenter__.return_value = db
    db.__aexit__.return_value = False
    return MagicMock(return_value=db), db


def _stub_credentials(monkeypatch, *, refresh_token: str | None = "R0"):
    """The row's two sealed blobs, as their decrypted selves."""
    stored = OAuthToken(
        access_token="old", token_type="Bearer", refresh_token=refresh_token,
    )
    client = OAuthClientInformationFull(
        client_id="cid", redirect_uris=["https://app/cb"],
        token_endpoint_auth_method="none",
    )
    monkeypatch.setattr(
        refresher, "decrypt_secret", lambda _c: client.model_dump_json(),
    )
    monkeypatch.setattr(refresher.grant, "stored_tokens", lambda _row: stored)
    monkeypatch.setattr(refresher.grant, "store_tokens", MagicMock())
    return client


class TestRotatingOne:
    async def test_a_due_grant_is_rotated_and_persisted(self, monkeypatch):
        _stub_credentials(monkeypatch)
        connector = _connector()
        session, _db = _session_yielding(connector)
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)
        rotated = OAuthToken(access_token="new", token_type="Bearer", expires_in=3600)
        monkeypatch.setattr(
            refresher.oauth, "refresh_tokens", AsyncMock(return_value=rotated),
        )

        assert await refresher._refresh_one(1) is True

        refresher.grant.store_tokens.assert_called_once_with(
            connector, rotated, rotation=True,
        )

    async def test_a_row_another_replica_holds_is_left_alone(self, monkeypatch):
        # ``FOR UPDATE SKIP LOCKED`` returns nothing rather than queueing behind
        # whoever is already talking to the authorization server.
        session, _db = _session_yielding(None)
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)
        monkeypatch.setattr(refresher.oauth, "refresh_tokens", AsyncMock())

        assert await refresher._refresh_one(1) is False

        refresher.oauth.refresh_tokens.assert_not_called()

    async def test_a_grant_a_peer_already_rotated_is_not_rotated_again(
        self, monkeypatch,
    ):
        # The batch is serial, so by the time a late row is reached another
        # replica may have refreshed it — doing it again would spend a
        # perfectly good refresh token for nothing.
        _stub_credentials(monkeypatch)
        fresh = _connector(
            oauth_tokens_expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
        )
        session, _db = _session_yielding(fresh)
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)
        monkeypatch.setattr(refresher.oauth, "refresh_tokens", AsyncMock())

        assert await refresher._refresh_one(1) is False

        refresher.oauth.refresh_tokens.assert_not_called()

    async def test_a_provider_outage_leaves_the_row_for_the_next_tick(
        self, monkeypatch,
    ):
        # Unreachable says nothing about the grant. Flagging it here would send
        # the user to the browser to fix someone else's downtime.
        _stub_credentials(monkeypatch)
        connector = _connector()
        session, _db = _session_yielding(connector)
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)
        monkeypatch.setattr(
            refresher.oauth, "refresh_tokens",
            AsyncMock(side_effect=oauth.ConnectorOAuthError("token endpoint down")),
        )

        assert await refresher._refresh_one(1) is False

        assert connector.oauth_reauthorization_required is False
        refresher.grant.store_tokens.assert_not_called()


class TestARotationThatFailsToLand:
    """The one write that cannot be retried later: the provider has already
    consumed the old refresh token, so its replacement exists only in memory."""

    def _pending(self, **over) -> refresher._Rotation:
        base = dict(
            connector_id=1, ciphertext="the-rotation",
            expires_at=datetime.now(timezone.utc),
            refresh_after=datetime.now(timezone.utc) + timedelta(seconds=150),
            spent="the-spent-one",
        )
        base.update(over)
        return refresher._Rotation(**base)

    async def test_the_retry_runs_after_the_failed_session_is_closed(
        self, monkeypatch,
    ):
        # Retrying inside it would take a second lock on the same row while the
        # first session still holds it — a wait nobody can end, because the
        # holder is waiting for the retry to return.
        _stub_credentials(monkeypatch)
        monkeypatch.setattr(
            refresher.grant, "store_tokens",
            lambda row, _t, **_kw: setattr(
                row, "oauth_tokens_encrypted", "the-rotation",
            ),
        )
        connector = _connector(oauth_tokens_encrypted="the-spent-one")
        first, first_db = _session_yielding(connector)
        first_db.commit.side_effect = RuntimeError("connection reset")
        order: list[str] = []
        first_db.rollback.side_effect = lambda: order.append("rollback")
        first_db.__aexit__.side_effect = lambda *_a: order.append("closed")

        def second_session():
            order.append("retry opened")
            still_spent = _connector(oauth_tokens_encrypted="the-spent-one")
            return _session_yielding(still_spent)[1]

        sessions = iter([first_db, None])

        def next_session():
            got = next(sessions)
            return got if got is not None else second_session()

        monkeypatch.setattr(
            refresher, "AsyncSessionLocal", MagicMock(side_effect=next_session),
        )
        monkeypatch.setattr(
            refresher.oauth, "refresh_tokens",
            AsyncMock(return_value=OAuthToken(access_token="new", token_type="Bearer")),
        )

        assert await refresher._refresh_one(1) is True

        assert order == ["rollback", "closed", "retry opened"]

    async def test_a_failed_commit_is_written_again(self, monkeypatch):
        _stub_credentials(monkeypatch)

        def store(row, _tokens, **_kw):
            row.oauth_tokens_encrypted = "the-rotation"
            row.oauth_refresh_after = None
        monkeypatch.setattr(refresher.grant, "store_tokens", store)
        connector = _connector(oauth_tokens_encrypted="the-spent-one")
        # The retry re-reads in its own transaction, so it sees the row as the
        # database still has it — not the object whose commit just failed.
        reread = _connector(oauth_tokens_encrypted="the-spent-one")
        first, first_db = _session_yielding(connector)
        second, second_db = _session_yielding(reread)
        first_db.commit.side_effect = RuntimeError("connection reset")
        monkeypatch.setattr(
            refresher, "AsyncSessionLocal",
            MagicMock(side_effect=[first_db, second_db]),
        )
        monkeypatch.setattr(
            refresher.oauth, "refresh_tokens",
            AsyncMock(return_value=OAuthToken(access_token="new", token_type="Bearer")),
        )

        assert await refresher._refresh_one(1) is True

        assert reread.oauth_tokens_encrypted == "the-rotation"
        second_db.commit.assert_awaited_once()
        assert reread.oauth_tokens_encrypted == "the-rotation"
        # The schedule travels with it. Landing the grant alone leaves the row
        # due on the very next tick — the rotation happening twice, the second
        # one spending a refresh token seconds old.
        assert reread.oauth_refresh_after is not None

    async def test_a_first_commit_that_landed_is_not_written_over(self, monkeypatch):
        # It reached the database and lost the connection before saying so.
        # The row already holds this exact ciphertext, which is success.
        landed = _connector(oauth_tokens_encrypted="the-rotation")
        session, db = _session_yielding(landed)
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)

        await refresher._commit_again(self._pending())

        db.commit.assert_not_awaited()

    async def test_a_row_someone_else_moved_keeps_theirs(self, monkeypatch):
        # A re-authorization, most likely — and theirs is newer than ours.
        current = _connector(oauth_tokens_encrypted="somebody-elses")
        session, db = _session_yielding(current)
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)

        await refresher._commit_again(self._pending())

        assert current.oauth_tokens_encrypted == "somebody-elses"
        db.commit.assert_not_awaited()


class TestAskingForTheBrowser:
    """The one state that costs the user something, so it wants proof."""

    async def test_a_rejected_refresh_token_asks_for_re_authorization(
        self, monkeypatch,
    ):
        _stub_credentials(monkeypatch)
        connector = _connector()
        session, db = _session_yielding(connector)
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)
        monkeypatch.setattr(
            refresher.oauth, "refresh_tokens",
            AsyncMock(side_effect=oauth.GrantRevoked("invalid_grant")),
        )

        assert await refresher._refresh_one(1) is False

        assert connector.oauth_reauthorization_required is True
        db.commit.assert_awaited()

    async def test_a_grant_with_no_refresh_token_asks_too(self, monkeypatch):
        # Nothing here can ever renew it, and leaving the flag clear would have
        # every tick re-select the row to discover the same thing.
        _stub_credentials(monkeypatch, refresh_token=None)
        connector = _connector()
        session, _db = _session_yielding(connector)
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)
        monkeypatch.setattr(refresher.oauth, "refresh_tokens", AsyncMock())

        assert await refresher._refresh_one(1) is False

        assert connector.oauth_reauthorization_required is True
        refresher.oauth.refresh_tokens.assert_not_called()

    async def test_a_row_with_no_stored_metadata_is_skipped_silently(
        self, monkeypatch,
    ):
        # Guessing a token endpoint would post the refresh token — and the
        # client secret under it — to whatever host the Connector points at.
        _stub_credentials(monkeypatch)
        connector = _connector(oauth_metadata=None)
        session, _db = _session_yielding(connector)
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)
        monkeypatch.setattr(refresher.oauth, "refresh_tokens", AsyncMock())

        assert await refresher._refresh_one(1) is False

        assert connector.oauth_reauthorization_required is False
        refresher.oauth.refresh_tokens.assert_not_called()


class TestOneBadConnectorDoesNotStarveTheRest:
    """The due query sorts oldest-expiry-first and a failure changes nothing
    about the row, so without a backoff the same unwell rows are selected every
    tick — forever — and everything behind them is never refreshed at all."""

    async def test_a_failure_records_its_backoff_before_releasing_the_row(
        self, monkeypatch,
    ):
        # Under the lock, not from a transaction afterwards. A backoff written
        # later cannot tell whether a peer has since rotated this row and
        # scheduled it properly, and would overwrite that with a flat 5-minute
        # wait — long enough for a short-lived token to die twice over.
        _stub_credentials(monkeypatch)
        connector = _connector(oauth_refresh_after=None)
        session, db = _session_yielding(connector)
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)
        monkeypatch.setattr(
            refresher.oauth, "refresh_tokens",
            AsyncMock(side_effect=oauth.ConnectorOAuthError("endpoint down")),
        )
        before = datetime.now(timezone.utc)

        assert await refresher._refresh_one(1) is False

        assert connector.oauth_refresh_after is not None
        waited = connector.oauth_refresh_after - before
        assert abs(waited.total_seconds() - refresher._RETRY_AFTER_SECONDS) < 2
        db.commit.assert_awaited()

    async def test_the_query_excludes_them_rather_than_the_batch(self):
        # After the LIMIT is too late: those rows are the oldest-expiry ones,
        # so they would fill every slot and nothing behind them would be
        # selected at all.
        db = AsyncMock()
        db.execute.return_value = MagicMock(all=MagicMock(return_value=[]))

        await refresher._list_due(db)

        compiled = str(db.execute.call_args[0][0])
        assert "oauth_refresh_after" in compiled
        assert compiled.index("oauth_refresh_after") < compiled.index("LIMIT")


class TestTheUnderLockRecheck:
    """The lock decides whether a row selected by an earlier query still wants
    what that query thought it wanted."""

    async def test_a_schedule_a_peer_just_wrote_is_honoured(self, monkeypatch):
        # Both replicas take the same id from the same batch; A rotates and
        # schedules the next attempt; B then gets the lock. On expiry alone B
        # finds it "still due" — a 300-second token is inside the 600-second
        # look-ahead the moment it is written — and rotates it straight again,
        # spending a refresh token a second old.
        _stub_credentials(monkeypatch)
        connector = _connector(
            oauth_tokens_expires_at=(
                datetime.now(timezone.utc) + timedelta(seconds=300)
            ),
            oauth_refresh_after=(
                datetime.now(timezone.utc) + timedelta(seconds=150)
            ),
        )
        session, _db = _session_yielding(connector)
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)
        rotate = AsyncMock()
        monkeypatch.setattr(refresher.oauth, "refresh_tokens", rotate)

        assert await refresher._refresh_one(1) is False

        rotate.assert_not_awaited()

    async def test_a_lapsed_schedule_does_not_hold_it_back(self, monkeypatch):
        _stub_credentials(monkeypatch)
        connector = _connector(
            oauth_refresh_after=(
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ),
        )
        session, _db = _session_yielding(connector)
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)
        monkeypatch.setattr(
            refresher.oauth, "refresh_tokens",
            AsyncMock(return_value=OAuthToken(
                access_token="new", token_type="Bearer", expires_in=3600,
            )),
        )

        assert await refresher._refresh_one(1) is True


class TestCancellationMidRotation:
    """``asyncio.run`` installs its own SIGINT handler and cancels the main
    task, and a fatal sibling in the ``TaskGroup`` cancels this one — so a
    cancellation arriving between the provider's answer and the write is a live
    path, and ``CancelledError`` being a ``BaseException`` means it would sail
    past every ordinary handler on the way out."""

    def _rotating(self, monkeypatch, *, commit):
        _stub_credentials(monkeypatch)
        connector = _connector()
        session, db = _session_yielding(connector)
        db.commit.side_effect = commit
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)
        monkeypatch.setattr(
            refresher.oauth, "refresh_tokens",
            AsyncMock(return_value=OAuthToken(
                access_token="new", token_type="Bearer",
                refresh_token="R2", expires_in=3600,
            )),
        )
        return db

    async def test_the_rotated_grant_is_landed_before_the_cancel_continues(
        self, monkeypatch,
    ):
        # By now the provider has spent R1, so R2 is the only copy in
        # existence. Dropping it costs the user a trip through the browser for
        # a grant that was renewed perfectly well.
        self._rotating(monkeypatch, commit=asyncio.CancelledError())
        landed: list[object] = []

        async def second_attempt(pending):
            landed.append(pending)
        monkeypatch.setattr(refresher, "_commit_again", second_attempt)

        with pytest.raises(asyncio.CancelledError):
            await refresher._refresh_one(1)

        assert len(landed) == 1
        assert landed[0].ciphertext is not None

    async def test_the_cancellation_still_wins(self, monkeypatch):
        # Recovered from, not swallowed: the task must still stop.
        self._rotating(monkeypatch, commit=asyncio.CancelledError())

        async def never_returns(_pending):
            await asyncio.sleep(3600)
        monkeypatch.setattr(refresher, "_commit_again", never_returns)
        monkeypatch.setattr(refresher, "_SECOND_ATTEMPT_TIMEOUT", 0.01)

        with pytest.raises(asyncio.CancelledError):
            await refresher._refresh_one(1)


class TestARotationRecordsWhenToComeBack:
    """Computing the answer is worth nothing if the rotation does not store
    it — and a test that calls the helper directly cannot tell the two apart."""

    async def test_the_rotation_writes_it_onto_the_row(self, monkeypatch):
        _stub_credentials(monkeypatch)
        # The expiry a 300-second token leaves behind. ``store_tokens`` is a
        # stub here — what is under test is that the rotation asks when to come
        # back and writes the answer down, not how the grant is sealed.
        connector = _connector(
            oauth_tokens_expires_at=(
                datetime.now(timezone.utc) + timedelta(seconds=300)
            ),
            oauth_refresh_after=None,
        )
        session, _db = _session_yielding(connector)
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)
        monkeypatch.setattr(
            refresher.oauth, "refresh_tokens",
            AsyncMock(return_value=OAuthToken(
                access_token="new", token_type="Bearer",
                refresh_token="r2", expires_in=300,
            )),
        )

        assert await refresher._refresh_one(1)

        # Written, and to the half-life rather than the look-ahead: on expiry
        # alone a 300-second token is due again on the very next tick.
        assert connector.oauth_refresh_after is not None
        margin = connector.oauth_tokens_expires_at - connector.oauth_refresh_after
        assert timedelta(seconds=140) < margin < timedelta(seconds=160)


class TestWhenToComeBack:
    """``oauth_refresh_after`` is written from what the last attempt learned."""

    def _expiry_in(self, seconds: int) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=seconds)

    def test_a_short_lived_token_comes_back_at_half_its_life(self, monkeypatch):
        # Its new expiry is already inside the 600s look-ahead, so on expiry
        # alone it would be due again on the very next tick — a provider
        # refreshed once a minute for a token it reissues happily at half the
        # age, sitting ahead of every healthy row in the ordering.
        monkeypatch.setattr(
            refresher.settings, "connector_refresh_ahead_seconds", 600,
        )
        before = datetime.now(timezone.utc)

        when = refresher._next_attempt_after(self._expiry_in(300))

        assert timedelta(seconds=140) < when - before < timedelta(seconds=160)

    def test_it_never_comes_back_after_the_token_dies(self, monkeypatch):
        monkeypatch.setattr(
            refresher.settings, "connector_refresh_ahead_seconds", 600,
        )
        expires_at = self._expiry_in(300)

        assert refresher._next_attempt_after(expires_at) < expires_at

    def test_a_long_lived_token_keeps_the_configured_look_ahead(
        self, monkeypatch,
    ):
        # Unchanged behaviour where expiry alone was already the right answer:
        # an hour-long token is picked up 600s before it dies, not at 30 min.
        monkeypatch.setattr(
            refresher.settings, "connector_refresh_ahead_seconds", 600,
        )
        expires_at = self._expiry_in(3600)

        when = refresher._next_attempt_after(expires_at)

        assert abs((expires_at - when).total_seconds() - 600) < 2


class TestSelectingWhatIsDue:
    async def test_the_query_skips_rows_already_asking_for_a_browser(self):
        # Their refresh token is known dead; retrying it every tick would only
        # spend requests to be refused.
        db = AsyncMock()
        db.execute.return_value = MagicMock(all=MagicMock(return_value=[]))

        await refresher._list_due(db)

        compiled = str(db.execute.call_args[0][0])
        assert "oauth_reauthorization_required" in compiled
        assert "oauth_tokens_expires_at" in compiled
        assert "auth_type" in compiled


class TestTheLoop:
    async def test_one_unwell_connector_does_not_end_the_tick(self, monkeypatch):
        # ``_refresh_one`` owns its own failures precisely so a single bad row
        # cannot cost the rest of the batch its refresh.
        _stub_credentials(monkeypatch)
        session, _db = _session_yielding(_connector())
        monkeypatch.setattr(refresher, "AsyncSessionLocal", session)
        monkeypatch.setattr(
            refresher.oauth, "refresh_tokens",
            AsyncMock(side_effect=RuntimeError("pool exhausted")),
        )

        assert await refresher._refresh_one(1) is False
