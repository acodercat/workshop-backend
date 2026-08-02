"""Unit tests for the rate-limit key function.

Two layers:

- :func:`_rate_limit_key` — the public key. Authenticated requests key on
  ``user:{id}`` (NAT-immune); anonymous ones fall back to ``ip:{addr}``.
- :func:`_client_ip` — the IP fallback. Its ``X-Forwarded-For`` handling
  is security-relevant: trusting XFF when not behind a proxy lets a client
  spoof the key, so the default must be the socket peer.
"""

from app import rate_limit


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeState:
    """Stand-in for ``request.state``; ``user_id`` absent unless set."""


class _FakeRequest:
    def __init__(
        self, peer: str, headers: dict | None = None, user_id: int | None = None
    ) -> None:
        self.client = _FakeClient(peer)
        self.headers = headers or {}
        self.state = _FakeState()
        if user_id is not None:
            self.state.user_id = user_id


# --- _rate_limit_key: user when authenticated, else IP -----------------


def test_keys_on_user_when_authenticated() -> None:
    req = _FakeRequest("10.0.0.1", user_id=42)
    assert rate_limit._rate_limit_key(req) == "user:42"


def test_authenticated_user_takes_precedence_over_ip(monkeypatch) -> None:
    # Even with a trusted proxy header present, an authenticated request
    # keys on the user — its IP never enters the bucket name.
    monkeypatch.setattr(rate_limit.settings, "trust_proxy_headers", True)
    req = _FakeRequest("10.0.0.1", {"x-forwarded-for": "1.2.3.4"}, user_id=42)
    assert rate_limit._rate_limit_key(req) == "user:42"


def test_keys_on_ip_when_anonymous(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit.settings, "trust_proxy_headers", False)
    req = _FakeRequest("10.0.0.1")
    assert rate_limit._rate_limit_key(req) == "ip:10.0.0.1"


# --- _client_ip: the IP fallback's proxy-header handling ---------------


def test_uses_socket_peer_by_default(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit.settings, "trust_proxy_headers", False)
    req = _FakeRequest("10.0.0.1", {"x-forwarded-for": "1.2.3.4"})

    # XFF is ignored when proxy headers aren't trusted — no spoofing.
    assert rate_limit._client_ip(req) == "10.0.0.1"


def test_uses_first_xff_hop_when_trusted(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit.settings, "trust_proxy_headers", True)
    req = _FakeRequest("10.0.0.1", {"x-forwarded-for": "1.2.3.4, 10.0.0.1"})

    assert rate_limit._client_ip(req) == "1.2.3.4"


def test_falls_back_to_peer_when_trusted_but_no_xff(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit.settings, "trust_proxy_headers", True)
    req = _FakeRequest("10.0.0.1", {})

    assert rate_limit._client_ip(req) == "10.0.0.1"
