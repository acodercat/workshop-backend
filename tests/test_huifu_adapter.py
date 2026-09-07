"""Offline unit tests for the 汇付 adapter — signature verify, ack, and
the response→event mapping. The SDK's network call is monkeypatched; RSA
uses a throwaway test keypair."""

import json

import pytest
from Crypto.PublicKey import RSA
from dg_sdk.core.rsa_utils import rsa_sign

from app.errors import PaymentProviderError
from app.services.payment import huifu
from app.services.payment.exceptions import NotifySignatureError
from app.services.payment.huifu import HuifuGateway
from app.services.payment.provider import ChargeState
from config import settings


@pytest.fixture
def keypair(monkeypatch):
    key = RSA.generate(2048)
    priv_pem = key.export_key(pkcs=8).decode()
    pub_pem = key.publickey().export_key().decode()
    monkeypatch.setattr(settings, "huifu_rsa_public_key", pub_pem)
    return priv_pem, pub_pem


def _signed(resp_data: str, priv_pem: str) -> str:
    ok, sign = rsa_sign(priv_pem, resp_data)
    assert ok
    return sign


def test_verify_notify_accepts_valid_signature(keypair) -> None:
    priv_pem, _ = keypair
    resp_data = json.dumps(
        {
            "req_seq_id": "hf1",
            "hf_seq_id": "H9",
            "sub_resp_code": "00000000",
            "trans_stat": "S",
            "trans_amt": "100.00",
        }
    )
    event = HuifuGateway().verify_notify(
        resp_data_raw=resp_data,
        sign=_signed(resp_data, priv_pem),
    )
    assert event.state is ChargeState.PAID
    assert event.out_trade_no == "hf1"
    assert event.provider_txn_id == "H9"
    assert event.amount_fen == 10000


def test_verify_notify_rejects_tampered_body(keypair) -> None:
    priv_pem, _ = keypair
    resp_data = json.dumps({"req_seq_id": "hf1", "trans_amt": "1.00"})
    sign = _signed(resp_data, priv_pem)
    tampered = json.dumps({"req_seq_id": "hf1", "trans_amt": "999.00"})
    with pytest.raises(NotifySignatureError):
        HuifuGateway().verify_notify(resp_data_raw=tampered, sign=sign)


def test_ack_format() -> None:
    assert HuifuGateway().ack("hfABC") == "RECV_ORD_ID_hfABC"


def test_event_mapping_reads_processing_as_no_verdict() -> None:
    event = huifu._event_from_business_data(
        {"req_seq_id": "hf2", "sub_resp_code": "00000000", "trans_stat": "P", "trans_amt": "5.00"}
    )
    assert event.state is ChargeState.PROCESSING  # trans_stat 'P' = 处理中
    assert event.amount_fen == 500


def test_event_mapping_reads_f_as_failed() -> None:
    event = huifu._event_from_business_data(
        {"req_seq_id": "hf3", "sub_resp_code": "10000001", "trans_stat": "F"}
    )
    assert event.state is ChargeState.FAILED


def test_event_missing_or_unknown_trans_stat_is_no_verdict() -> None:
    # An "accepted" payload with no trans_stat must read as neither paid
    # nor failed — there is no default-to-success, and no default-to-dead.
    for data in (
        {"req_seq_id": "hf9", "sub_resp_code": "00000000", "trans_amt": "1.00"},
        {"req_seq_id": "hf9", "sub_resp_code": "00000000", "trans_stat": "X"},
    ):
        assert huifu._event_from_business_data(data).state is ChargeState.PROCESSING


def test_require_accepted_raises_on_non_dict() -> None:
    with pytest.raises(PaymentProviderError):
        huifu._require_accepted("404 not found", op="create")


def test_require_accepted_allows_processing_code() -> None:
    # 00000100 (受理成功/待支付) is the normal scan-pay create code — accepted.
    data = {"resp_code": "00000100", "qr_code": "https://qr/x"}
    assert huifu._require_accepted(data, op="create") is data


def test_require_accepted_raises_on_rejected_code() -> None:
    with pytest.raises(PaymentProviderError):
        huifu._require_accepted(
            {"resp_code": "20000000", "resp_desc": "nope"},
            op="create",
        )


async def test_create_charge_maps_amount_and_extracts_qr(monkeypatch) -> None:
    monkeypatch.setattr(huifu, "_ensure_client", lambda: None)
    captured = {}

    def fake_post(self, extend_infos):
        captured["trans_amt"] = self.trans_amt
        captured["trade_type"] = self.trade_type
        captured["req_seq_id"] = self.req_seq_id
        captured["time_expire"] = extend_infos.get("time_expire")
        # 00000100 = 受理成功/待支付 — the real scan-pay create response code.
        return {
            "resp_code": "00000100",
            "resp_desc": "下单成功",
            "qr_code": "https://qr/x",
            "hf_seq_id": "H1",
        }

    monkeypatch.setattr(huifu.dg_sdk.V2TradePaymentJspayRequest, "post", fake_post)

    result = await HuifuGateway().create_charge(
        out_trade_no="hf3",
        amount_fen=12345,
        channel="ALIPAY",
        goods_desc="x",
        req_date="20260616",
        time_expire="20260616120000",
    )
    assert result.qr_code == "https://qr/x"
    assert result.trade_type == "A_NATIVE"
    assert result.provider_txn_id == "H1"
    assert captured == {
        "trans_amt": "123.45",
        "trade_type": "A_NATIVE",
        "req_seq_id": "hf3",
        "time_expire": "20260616120000",
    }


async def test_sdk_exception_is_a_provider_error_on_query(monkeypatch) -> None:
    # A transport failure inside dg-sdk (connection refused, timeout) is the
    # same thing to a caller as a gateway rejection: the provider could not
    # be asked. One exception class for both — the reconciler reads it as
    # "no answer", never as "not paid".
    monkeypatch.setattr(huifu, "_ensure_client", lambda: None)

    def raising_post(self, extend_infos):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(huifu.dg_sdk.V2TradePaymentScanpayQueryRequest, "post", raising_post)
    with pytest.raises(PaymentProviderError) as info:
        await HuifuGateway().query_charge(out_trade_no="hf5", req_date="20260616")
    assert isinstance(info.value.__cause__, ConnectionError)


async def test_sdk_exception_is_a_provider_error_on_create(monkeypatch) -> None:
    # The customer-facing path: an untranslated SDK exception here would be
    # a 500 instead of the 502 ``PaymentProviderError`` carries.
    monkeypatch.setattr(huifu, "_ensure_client", lambda: None)

    def raising_post(self, extend_infos):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(huifu.dg_sdk.V2TradePaymentJspayRequest, "post", raising_post)
    with pytest.raises(PaymentProviderError) as info:
        await HuifuGateway().create_charge(
            out_trade_no="hf6",
            amount_fen=100,
            channel="ALIPAY",
            goods_desc="x",
            req_date="20260616",
            time_expire="20260616120000",
        )
    assert isinstance(info.value.__cause__, TimeoutError)


async def test_create_charge_raises_without_qr(monkeypatch) -> None:
    monkeypatch.setattr(huifu, "_ensure_client", lambda: None)
    monkeypatch.setattr(
        huifu.dg_sdk.V2TradePaymentJspayRequest,
        "post",
        lambda self, extend_infos: {"resp_code": "00000000", "sub_resp_desc": "no qr"},
    )
    with pytest.raises(PaymentProviderError):
        await HuifuGateway().create_charge(
            out_trade_no="hf4",
            amount_fen=100,
            channel="WECHAT",
            goods_desc="x",
            req_date="20260616",
            time_expire="20260616120000",
        )
