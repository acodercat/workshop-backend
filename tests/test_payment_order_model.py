"""Offline unit tests for the payment_order model + money conversions."""

from app.models.payment_order import PaymentOrderStatus, PaymentProvider
from app.services.credits.rates import (
    _MICRO_PER_CREDIT,
    credits_from_cny,
    micro_from_credits,
)
from app.services.payment.huifu import _fen_to_yuan, _yuan_to_fen


def test_enum_values_are_uppercase_member_names() -> None:
    # The Postgres enum stores the member *values*; values_callable in the
    # model relies on these being the UPPERCASE labels.
    assert [member.value for member in PaymentOrderStatus] == [
        "PENDING",
        "PAID",
        "EXPIRED",
        "FAILED",
    ]
    assert [member.value for member in PaymentProvider] == ["HUIFU"]


def test_fen_yuan_roundtrip() -> None:
    assert _fen_to_yuan(10000) == "100.00"
    assert _fen_to_yuan(1) == "0.01"
    assert _fen_to_yuan(199) == "1.99"
    for fen in (1, 199, 10000, 999999):
        assert _yuan_to_fen(_fen_to_yuan(fen)) == fen


def test_credit_grant_is_frozen_at_sale_price() -> None:
    # ¥100 at the default ¥0.1/credit sale price → 1000 whole credits.
    credits = credits_from_cny(100)
    assert credits == 1000
    # …frozen onto the order as µ-credits.
    assert micro_from_credits(credits) == 1000 * _MICRO_PER_CREDIT
