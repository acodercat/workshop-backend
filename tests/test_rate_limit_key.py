"""Unit tests for the rate-limit client-IP key function.

The key decides which bucket a request counts against, so its handling of
``X-Forwarded-For`` is security-relevant: trusting XFF when not behind a
proxy lets a client spoof the key. Default must be the socket peer.
"""

from app import rate_limit


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, peer: str, headers: dict | None = None) -> None:
        self.client = _FakeClient(peer)
        self.headers = headers or {}


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
