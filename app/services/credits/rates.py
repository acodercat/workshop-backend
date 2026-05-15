"""Rate-card lookup + cost arithmetic.

The rate card lives in :attr:`config.Settings.credit_rates` —
edit-and-restart for V1. Each entry is snapshotted onto the
:class:`~app.models.credit_reservation.CreditReservation` at reserve
time so a config change between reserve and settle does not re-price
the turn (the customer-trust property: the cost shown at start is
the cost actually charged).

Everything in this module deals in **µ-credits** (the storage unit;
1 credit = 1_000_000 µ-credits, 1 credit ≈ $0.001 USD).
"""

import math
from typing import TypedDict

from config import settings

# 1 USD of LLM cost == this many µ-credits, given the peg
# (1 credit = $0.001, 1 credit = 1_000_000 µ-credits → $1 = 1e9 µ-credits).
_USD_TO_MICRO_CREDITS = 1_000_000_000


class RateSnapshot(TypedDict):
    """Immutable copy of one rate-card row, written verbatim onto a
    reservation so settle can compute cost without re-reading config.

    Keys match :attr:`config.Settings.credit_rates` plus a
    ``captured_at`` ISO timestamp so the row tells you exactly which
    point-in-time card it was charged against.
    """

    model_id: str
    in_per_1k_usd: float
    out_per_1k_usd: float
    margin: float
    max_output_tokens: int
    captured_at: str


def snapshot_rate(model_id: str) -> RateSnapshot:
    """Return a fresh snapshot of the current rate-card row for
    ``model_id``. Raises ``KeyError`` if the model isn't priced —
    that's intentional: silently charging zero for an un-priced model
    is the kind of bug that costs money for months before anyone
    notices.
    """
    from datetime import datetime, timezone

    card = settings.credit_rates.get(model_id)
    if card is None:
        raise KeyError(
            f"No rate-card entry for model_id={model_id!r}; "
            f"add one to ``settings.credit_rates`` in config.py"
        )
    return RateSnapshot(
        model_id=model_id,
        in_per_1k_usd=float(card["in_per_1k_usd"]),
        out_per_1k_usd=float(card["out_per_1k_usd"]),
        margin=float(card["margin"]),
        max_output_tokens=int(card["max_output_tokens"]),
        captured_at=datetime.now(timezone.utc).isoformat(),
    )


def cost_micro_credits(
    rate: RateSnapshot, *, input_tokens: int, output_tokens: int,
) -> int:
    """Compute cost in µ-credits from token counts using ``rate``.

    Math: ``cost_usd = (in / 1k * in_rate + out / 1k * out_rate) * margin``,
    then convert to µ-credits via the USD peg.

    ``ceil`` rather than ``round`` so partial-credit fractions always
    go to the operator's side — billing-safe rounding.
    """
    cost_usd = (
        (input_tokens / 1000.0) * rate["in_per_1k_usd"]
        + (output_tokens / 1000.0) * rate["out_per_1k_usd"]
    ) * rate["margin"]
    return int(math.ceil(cost_usd * _USD_TO_MICRO_CREDITS))


def reserve_micro_credits(
    rate: RateSnapshot, *, max_input_tokens: int,
) -> int:
    """Compute the upfront reserve amount in µ-credits.

    Uses the rate's ``max_output_tokens`` as the upper bound for
    output. Input is the caller-supplied estimate (pessimistic; see
    :attr:`config.Settings.credit_reserve_max_input_tokens`).
    """
    return cost_micro_credits(
        rate,
        input_tokens=max_input_tokens,
        output_tokens=rate["max_output_tokens"],
    )


def credits_from_micro(amount_micro: int) -> int:
    """Convert µ-credits to whole-credit integer (display only)."""
    return amount_micro // 1_000_000


def micro_from_credits(amount_credits: int) -> int:
    """Convert whole-credit integer to µ-credits (storage)."""
    return amount_credits * 1_000_000
