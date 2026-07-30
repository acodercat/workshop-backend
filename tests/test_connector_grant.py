"""Tests for ``app.services.mcp.grant`` — writing a grant onto a Connector row."""

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from mcp.shared.auth import OAuthToken

from app.services.mcp import grant


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    from app.services import crypto

    monkeypatch.setattr(
        crypto.settings,
        "mcp_credential_encryption_key",
        Fernet.generate_key().decode(),
    )
    crypto._cipher.cache_clear()
    yield
    crypto._cipher.cache_clear()


def _connector():
    from app.models.mcp_server import MCPServer, MCPServerAuthType

    return MCPServer(
        id=1, user_id=7, name="c", url="https://mcp.example.com/mcp",
        auth_type=MCPServerAuthType.OAUTH,
    )


class TestALifetimeAlreadySpent:
    """``expires_in=0`` is a server saying its token is dead on arrival. That is
    a statement, not silence, and the two must not collapse into each other."""

    @pytest.mark.parametrize("expires_in", [0, -1_000_000_000])
    def test_it_is_due_immediately_rather_than_given_an_hour(self, expires_in):
        # Treating it as "unstated" would schedule the row an hour out, and
        # every turn in that hour would send a token known to be spent.
        connector = _connector()
        before = datetime.now(timezone.utc)

        grant.store_tokens(
            connector,
            OAuthToken(
                access_token="a", token_type="Bearer",
                refresh_token="r", expires_in=expires_in,
            ),
            rotation=False,
        )

        assert connector.oauth_tokens_expires_at <= datetime.now(timezone.utc)
        # Clamped, not applied: a deadline decades back is unrecoverable, since
        # the refresher orders by this column and such a row would be the most
        # overdue thing in the queue forever.
        assert connector.oauth_tokens_expires_at >= before

    def test_silence_is_not_the_same_as_zero(self):
        # No statement at all, with a refresh token to act on: assume the
        # common hour rather than declaring it spent.
        connector = _connector()

        grant.store_tokens(
            connector,
            OAuthToken(access_token="a", token_type="Bearer", refresh_token="r"),
            rotation=False,
        )

        assert connector.oauth_tokens_expires_at > datetime.now(timezone.utc)

    def test_with_no_refresh_token_silence_leaves_it_unscheduled(self):
        # Nothing could act on it: the refresher would find nothing to rotate
        # with and ask for a browser every hour, forever.
        connector = _connector()

        grant.store_tokens(
            connector,
            OAuthToken(access_token="a", token_type="Bearer"),
            rotation=False,
        )

        assert connector.oauth_tokens_expires_at is None


class TestAStaleSchedule:
    """``oauth_refresh_after`` was reasoned from the grant being replaced."""

    def test_a_new_grant_does_not_inherit_the_old_one_s_backoff(self):
        # The authorization callback has no schedule of its own to write. A
        # user who re-authorizes during a five-minute backoff would otherwise
        # hand their brand-new grant the dead one's wait.
        connector = _connector()
        connector.oauth_refresh_after = datetime.now(timezone.utc) + timedelta(
            seconds=300
        )

        grant.store_tokens(
            connector,
            OAuthToken(access_token="a", token_type="Bearer", refresh_token="r"),
            rotation=False,
        )

        assert connector.oauth_refresh_after is None


class TestWhichCallerIsWriting:
    """A refresh response may mean "keep using yours"; a code exchange never
    does — it establishes a whole new grant."""

    def test_a_rotation_keeps_a_refresh_token_the_response_omitted(self):
        connector = _connector()
        grant.store_tokens(
            connector,
            OAuthToken(access_token="a", token_type="Bearer", refresh_token="r1"),
            rotation=False,
        )

        grant.store_tokens(
            connector,
            OAuthToken(access_token="b", token_type="Bearer"),
            rotation=True,
        )

        assert grant.stored_tokens(connector).refresh_token == "r1"

    def test_an_authorization_does_not_inherit_the_old_one(self):
        # It may be a re-authorization against a different authorization
        # server; carrying the old refresh token across would hand that server
        # a credential belonging to the previous one.
        connector = _connector()
        grant.store_tokens(
            connector,
            OAuthToken(access_token="a", token_type="Bearer", refresh_token="r1"),
            rotation=False,
        )

        grant.store_tokens(
            connector,
            OAuthToken(access_token="b", token_type="Bearer"),
            rotation=False,
        )

        assert grant.stored_tokens(connector).refresh_token is None
