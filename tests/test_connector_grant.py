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
        )

        assert connector.oauth_tokens_expires_at <= datetime.now(timezone.utc)
        # Clamped, not applied: a nonsense lifetime should read as "expired now",
        # not as a date far enough out of range to be mistaken for meaningful.
        assert connector.oauth_tokens_expires_at >= before

    def test_silence_is_not_the_same_as_zero(self):
        # No statement at all, with a refresh token to act on: assume the
        # common hour rather than declaring it spent.
        connector = _connector()

        grant.store_tokens(
            connector,
            OAuthToken(access_token="a", token_type="Bearer", refresh_token="r"),
        )

        assert connector.oauth_tokens_expires_at > datetime.now(timezone.utc)

    def test_with_no_refresh_token_silence_leaves_it_unscheduled(self):
        # Nothing could act on it: renewal would find nothing to rotate with and
        # send the user to the browser for a token that had not expired.
        connector = _connector()

        grant.store_tokens(
            connector,
            OAuthToken(access_token="a", token_type="Bearer"),
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
        )

        assert connector.oauth_refresh_after is None


class TestWhatAnAuthorizationWrites:
    """Verbatim, always. A refresh response may mean "keep using yours" and has
    to be reconciled, but that is renewal's job and renewal is the worker's; a
    code exchange establishes a whole new grant."""

    def test_an_authorization_does_not_inherit_the_old_one(self):
        # It may be a re-authorization against a different authorization
        # server; carrying the old refresh token across would hand that server
        # a credential belonging to the previous one.
        connector = _connector()
        grant.store_tokens(
            connector,
            OAuthToken(access_token="a", token_type="Bearer", refresh_token="r1"),
        )

        grant.store_tokens(
            connector,
            OAuthToken(access_token="b", token_type="Bearer"),
        )

        assert grant.stored_tokens(connector).refresh_token is None


class TestGivingAGrantUp:
    """Disconnecting hands the grant back rather than shelving it, so what
    ``discard`` leaves behind decides what reconnecting costs."""

    def test_nothing_store_tokens_wrote_survives_it(self):
        # Asserted against ``store_tokens``'s own writes rather than a hand-kept
        # list: the two are inverses, and a column added to one and not the
        # other is exactly the bug worth catching.
        connector = _connector()
        grant.store_tokens(
            connector,
            OAuthToken(
                access_token="a", token_type="Bearer",
                refresh_token="r", expires_in=3600,
            ),
        )
        connector.oauth_refresh_after = datetime.now(timezone.utc)
        connector.oauth_reauthorization_required = True

        grant.discard(connector)

        assert connector.oauth_tokens_encrypted is None
        assert connector.oauth_tokens_expires_at is None
        assert connector.oauth_refresh_after is None
        assert connector.oauth_reauthorization_required is False

    def test_what_the_connector_needs_to_authorize_again_is_kept(self):
        # These belong to the Connector, not to any one grant. Dropping them
        # would make every reconnect a fresh dynamic registration against a
        # provider that already knows us.
        connector = _connector()
        connector.oauth_client_encrypted = "sealed-client"
        connector.oauth_scope = "files:read"
        connector.oauth_metadata = {"authorization_server": {"issuer": "https://as"}}

        grant.discard(connector)

        assert connector.oauth_client_encrypted == "sealed-client"
        assert connector.oauth_scope == "files:read"
        assert connector.oauth_metadata == {
            "authorization_server": {"issuer": "https://as"}
        }
