"""Provider-agnostic payment abstraction.

The order state machine (``orders.py``), endpoints, and reconciler
depend only on this ``PaymentGateway`` protocol and the two result
dataclasses — never on a concrete SDK. All 汇付 / ``dg-sdk`` specifics
live behind it in ``huifu.py``, so adding a second provider is a new
adapter plus a ``PaymentProvider`` enum member, with the rest of the
subsystem untouched. This mirrors how the agent library keeps provider
code behind the ``ExecutionEnvironment`` protocol.

Money crosses this boundary as integer **RMB cents** (``amount_fen``);
the yuan-string / decimal representation a provider's wire wants is the
adapter's concern.
"""

from dataclasses import dataclass
from typing import Any, Literal, Protocol

# The payment channel the user picked. Providers map it to their own
# trade-type codes (汇付: ALIPAY→A_NATIVE, WECHAT→W_NATIVE).
Channel = Literal["ALIPAY", "WECHAT"]


@dataclass(frozen=True)
class ChargeResult:
    """Outcome of placing an order with the provider.

    ``qr_code`` is the payment code-URL string the frontend renders as a
    scannable image. ``raw`` keeps the provider's verified response dict
    for persisting into ``payment_order.reference`` (debug/audit).
    """

    out_trade_no: str
    qr_code: str
    trade_type: str
    provider_txn_id: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class VerifiedEvent:
    """A signature-verified payment outcome, from either an async
    notification or a reconciliation query.

    ``succeeded`` is the provider's terminal success verdict (汇付:
    ``sub_resp_code == '00000000'`` / ``trans_stat == 'S'``).
    ``amount_fen`` is the provider-reported paid amount in RMB cents,
    compared against the order's frozen ``amount_fen`` to defend against
    a tampered callback. ``provider_txn_id`` is the provider's own id.
    """

    out_trade_no: str
    provider_txn_id: str | None
    amount_fen: int
    succeeded: bool
    raw: dict[str, Any]


class PaymentGateway(Protocol):
    """The contract every payment adapter implements.

    Implementations are blocking under the hood (SDKs use ``requests``);
    the async signatures here let the adapter hop to a thread
    (``asyncio.to_thread``) so callers stay non-blocking.
    """

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
        """Place an order and return a QR to display. ``time_expire`` is the
        provider-side payment deadline (so the QR stops being payable exactly
        when the order expires). Raises
        :class:`~app.errors.PaymentProviderError` on an upstream failure."""
        ...

    async def query_charge(self, *, out_trade_no: str, req_date: str) -> VerifiedEvent:
        """Query the provider for an order's current state (reconciliation)."""
        ...

    def verify_notify(self, *, resp_data_raw: str, sign: str) -> VerifiedEvent:
        """Verify an async-notification signature over the raw body string
        and parse it. Raises on signature failure — an unverifiable
        notification must never be acted on."""
        ...

    def ack(self, out_trade_no: str) -> str:
        """The exact body string the provider expects as a delivery
        acknowledgement (汇付: ``RECV_ORD_ID_`` + request id)."""
        ...
