"""汇付斗拱 (Huifu Dougong) payment adapter — the only place ``dg-sdk``
and Huifu wire details live.

Implements the :class:`~app.services.payment.provider.PaymentGateway`
protocol. The SDK (`dg_sdk`) is synchronous (`requests` under the hood),
so every network call hops to a thread via ``asyncio.to_thread`` to keep
the async callers non-blocking — this is the one place in the repo that
needs a sync→async bridge, and it's deliberately confined here.

Signing/verification facts (read from ``dg_sdk`` 2.0.22):

- ``MerConfig(private_key, public_key, sys_id, product_id)`` +
  ``DGClient.env`` (``"prod"`` → api.huifu.com / ``"mertest"`` →
  sandbox). ``huifu_id`` is a per-request field, not part of init.
- Outbound requests are RSA-signed by the SDK; responses are
  RSA-verified by the SDK, which returns the inner ``data`` dict on
  success or a non-dict (Response / text) on failure.
- The **async notification** arrives as form-urlencoded ``resp_data``
  (a JSON string) + ``sign``. We verify the signature over the **raw
  ``resp_data`` string** with ``rsa_design`` (Huifu's own helper) — NOT
  by re-serializing a parsed dict, which could differ byte-for-byte from
  what Huifu signed and either spuriously fail or open a tamper gap.
"""

import asyncio
import json
import logging
import threading
from decimal import Decimal
from typing import Any

import dg_sdk
from dg_sdk.core.rsa_utils import rsa_design

from app.errors import PaymentProviderError
from app.services.payment.exceptions import NotifySignatureError
from app.services.payment.provider import Channel, ChargeResult, VerifiedEvent
from config import settings

logger = logging.getLogger(__name__)

# Payment-success code in ``sub_resp_code`` + ``trans_stat == 'S'`` mark a
# completed (paid) transaction (used on the notify / query path).
_SUCCESS_CODE = "00000000"
_SUCCESS_TRANS_STAT = "S"

# Gateway-level "request accepted" codes in ``resp_code``. ``00000000`` =
# success; ``00000100`` = 受理成功/处理中 — the NORMAL response when placing a
# scan-pay order (QR issued, payment still pending). Both mean "proceed".
_ACCEPTED_RESP_CODES = frozenset({_SUCCESS_CODE, "00000100"})

# ALIPAY/WeChat "正扫" (merchant shows QR, customer scans) trade types.
_TRADE_TYPE_BY_CHANNEL: dict[Channel, str] = {
    "ALIPAY": "A_NATIVE",
    "WECHAT": "T_NATIVE",
}

# ``DGClient.mer_config`` is a process-global class attribute; initialise it
# once under a lock (double-checked), like ``file_storage.get_bucket``'s
# cached client. Guarded so an unconfigured dev box never touches Huifu.
_init_lock = threading.Lock()
_initialised = False


def _ensure_client() -> None:
    global _initialised
    if _initialised:
        return
    with _init_lock:
        if _initialised:
            return
        if not (
            settings.huifu_rsa_private_key
            and settings.huifu_rsa_public_key
            and settings.huifu_sys_id
            and settings.huifu_product_id
        ):
            raise PaymentProviderError("Huifu credentials are not configured")
        dg_sdk.DGClient.mer_config = dg_sdk.MerConfig(
            private_key=settings.huifu_rsa_private_key,
            public_key=settings.huifu_rsa_public_key,
            sys_id=settings.huifu_sys_id,
            product_id=settings.huifu_product_id,
        )
        dg_sdk.DGClient.env = settings.huifu_env
        _initialised = True


def _fen_to_yuan(amount_fen: int) -> str:
    """RMB cents → the 2-decimal yuan string Huifu's ``trans_amt`` wants."""
    return str((Decimal(amount_fen) / 100).quantize(Decimal("0.01")))


def _yuan_to_fen(trans_amt: str) -> int:
    """Huifu yuan string → RMB cents (exact, via Decimal — never float)."""
    return int((Decimal(str(trans_amt)) * 100).to_integral_value())


class HuifuGateway:
    """``PaymentGateway`` over 汇付斗拱 via ``dg-sdk``."""

    async def create_charge(
        self,
        *,
        out_trade_no: str,
        amount_fen: int,
        channel: Channel,
        goods_desc: str,
        req_date: str,
        time_expire: str,
    ) -> ChargeResult:
        trade_type = _TRADE_TYPE_BY_CHANNEL[channel]
        data = await asyncio.to_thread(
            self._create_charge_sync,
            out_trade_no=out_trade_no,
            amount_fen=amount_fen,
            trade_type=trade_type,
            goods_desc=goods_desc,
            req_date=req_date,
            time_expire=time_expire,
        )
        qr_code = data.get("qr_code")
        if not qr_code:
            raise PaymentProviderError(
                f"Huifu create returned no qr_code "
                f"(sub_resp_desc={data.get('sub_resp_desc')!r})"
            )
        return ChargeResult(
            out_trade_no=out_trade_no,
            qr_code=qr_code,
            trade_type=trade_type,
            provider_txn_id=data.get("hf_seq_id"),
            raw=data,
        )

    def _create_charge_sync(
        self,
        *,
        out_trade_no: str,
        amount_fen: int,
        trade_type: str,
        goods_desc: str,
        req_date: str,
        time_expire: str,
    ) -> dict[str, Any]:
        _ensure_client()
        # 聚合正扫 (NATIVE) via the v2 jspay endpoint — the universally
        # available 斗拱 scan-pay API (the v4 unified create requires a
        # per-merchant enablement many accounts don't have → 404).
        req = dg_sdk.V2TradePaymentJspayRequest()
        req.req_seq_id = out_trade_no
        req.req_date = req_date
        req.huifu_id = settings.huifu_merchant_id
        req.trade_type = trade_type
        req.trans_amt = _fen_to_yuan(amount_fen)
        req.goods_desc = goods_desc
        # Optional params ride in extend_infos. ``time_expire`` (yyyyMMddHHmmss)
        # makes Huifu stop accepting payment exactly when we expire the order.
        extend_infos: dict[str, Any] = {"time_expire": time_expire}
        if settings.huifu_notify_url:
            extend_infos["notify_url"] = settings.huifu_notify_url
        result = req.post(extend_infos)
        return _require_accepted(result, op="create")

    async def query_charge(self, *, out_trade_no: str, req_date: str) -> VerifiedEvent:
        data = await asyncio.to_thread(
            self._query_charge_sync, out_trade_no=out_trade_no, req_date=req_date,
        )
        return _event_from_business_data(data)

    def _query_charge_sync(self, *, out_trade_no: str, req_date: str) -> dict[str, Any]:
        _ensure_client()
        req = dg_sdk.V2TradePaymentScanpayQueryRequest()
        req.huifu_id = settings.huifu_merchant_id
        req.org_req_seq_id = out_trade_no
        req.org_req_date = req_date
        result = req.post({})
        return _require_accepted(result, op="query")

    def verify_notify(self, *, resp_data_raw: str, sign: str) -> VerifiedEvent:
        if not settings.huifu_rsa_public_key:
            raise PaymentProviderError("Huifu public key is not configured")
        ok, info = rsa_design(sign, resp_data_raw, settings.huifu_rsa_public_key)
        if not ok:
            raise NotifySignatureError(f"Huifu notify signature invalid: {info}")
        try:
            data = json.loads(resp_data_raw)
        except (ValueError, TypeError) as exc:
            raise NotifySignatureError(f"Huifu notify resp_data not JSON: {exc}") from exc
        return _event_from_business_data(data)

    def ack(self, out_trade_no: str) -> str:
        return "RECV_ORD_ID_" + out_trade_no


def _require_accepted(result: Any, *, op: str) -> dict[str, Any]:
    """The SDK returns the verified ``data`` dict on success, or a non-dict
    (``requests.Response`` / text) on HTTP / parse failure. Normalise to a
    dict whose gateway ``resp_code`` was accepted, else raise."""
    if not isinstance(result, dict):
        # dg-sdk returns the raw ``requests.Response`` on non-200, or the
        # response text on a JSON-parse failure. Log the HTTP status + body
        # so an upstream rejection (bad signature, missing field, merchant
        # not enabled, IP restriction…) is diagnosable instead of opaque.
        status = getattr(result, "status_code", None)
        body = getattr(result, "text", None)
        url = getattr(result, "url", None)
        headers = dict(getattr(result, "headers", {}) or {})
        if body is None and isinstance(result, str):
            body = result
        logger.error(
            "huifu %s rejected: http_status=%s url=%s body=%s headers=%s",
            op, status, url, (body or "")[:1000], headers,
        )
        raise PaymentProviderError(f"Huifu {op} failed (status={status})")
    resp_code = result.get("resp_code")
    if resp_code not in _ACCEPTED_RESP_CODES:
        logger.error(
            "huifu %s: resp_code=%s desc=%s sub=%s",
            op, resp_code, result.get("resp_desc"), result.get("sub_resp_desc"),
        )
        raise PaymentProviderError(
            f"Huifu {op} rejected (resp_code={resp_code!r}, "
            f"desc={result.get('resp_desc')!r})"
        )
    return result


def _event_from_business_data(data: dict[str, Any]) -> VerifiedEvent:
    """Map a Huifu business-data dict (from notify or query) to a
    :class:`VerifiedEvent`."""
    # ``trans_stat`` is Huifu's authoritative transaction status (P=处理中,
    # S=成功, F=失败); ``S`` is the only "paid" signal. No default — a
    # missing/unknown status reads as NOT succeeded (never grant on an
    # ambiguous payload).
    succeeded = data.get("trans_stat") == _SUCCESS_TRANS_STAT
    trans_amt = data.get("trans_amt")
    amount_fen = _yuan_to_fen(trans_amt) if trans_amt else 0
    return VerifiedEvent(
        out_trade_no=data.get("req_seq_id", ""),
        provider_txn_id=data.get("hf_seq_id"),
        amount_fen=amount_fen,
        succeeded=succeeded,
        raw=data,
    )
