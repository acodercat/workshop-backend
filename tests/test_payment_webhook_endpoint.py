"""Offline unit tests for the webhook endpoint's parse/verify/ack decision.

Calls the handler coroutine directly with a fake request + monkeypatched
provider/handler (no app boot, no DB/Redis), pinning the contract: ack
only after a verified + processed notification; 400 with no ack on a bad
or missing signature."""

import urllib.parse
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.api.public.payment as ep
from app.services.payment.exceptions import NotifySignatureError
from app.services.payment.provider import ChargeState, VerifiedEvent


class _FakeRequest:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _FakeProvider:
    def verify_notify(self, *, resp_data_raw: str, sign: str) -> VerifiedEvent:
        if sign == "bad":
            raise NotifySignatureError("bad signature")
        return VerifiedEvent("hf1", "H9", 100, state=ChargeState.PAID, raw={})

    def ack(self, out_trade_no: str) -> str:
        return "RECV_ORD_ID_" + out_trade_no


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(ep.payment, "get_provider", lambda: _FakeProvider())
    monkeypatch.setattr(ep.payment, "handle_webhook", AsyncMock())
    # The post-commit referral-commission hook is a separate concern; stub
    # it so this test pins only the webhook's parse/verify/ack contract
    # (and so it's independent of whether ``commission_enabled`` is set).
    monkeypatch.setattr(ep.commission, "accrue_for_order", AsyncMock())


def _db() -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    return db


async def test_valid_notify_returns_ack(patched) -> None:
    body = urllib.parse.urlencode({"resp_data": "{}", "sign": "ok"}).encode()
    resp = await ep.huifu_webhook(_FakeRequest(body), _db())
    assert resp.status_code == 200
    assert resp.body == b"RECV_ORD_ID_hf1"


async def test_bad_signature_400_without_ack(patched) -> None:
    body = urllib.parse.urlencode({"resp_data": "{}", "sign": "bad"}).encode()
    resp = await ep.huifu_webhook(_FakeRequest(body), _db())
    assert resp.status_code == 400
    assert b"RECV_ORD_ID" not in resp.body


async def test_missing_fields_400(patched) -> None:
    resp = await ep.huifu_webhook(_FakeRequest(b""), _db())
    assert resp.status_code == 400
