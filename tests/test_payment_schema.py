"""``CreateOrderRequest`` — the top-up amount has one exact value.

Before, ``amount_cny`` was a float rounded twice: half-even into the cents
charged, floored into the credits granted. ``10.096`` charged ¥10.10 and
granted 100 credits.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.payment import CreateOrderRequest


def test_an_amount_past_two_decimals_is_refused() -> None:
    with pytest.raises(ValidationError):
        CreateOrderRequest(amount_cny=10.096, channel="ALIPAY")


def test_a_two_decimal_amount_is_kept_exact() -> None:
    assert CreateOrderRequest(amount_cny=10.1, channel="ALIPAY").amount_cny == Decimal("10.1")
