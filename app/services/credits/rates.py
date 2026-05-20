"""Rate-card lookup + cost arithmetic.

The rate card lives in :attr:`config.Settings.credit_rates` —
edit-and-restart for V1. Each entry is snapshotted onto the
:class:`~app.models.credit_reservation.CreditReservation` at reserve
time so a config change between reserve and settle does not re-price
the turn (the customer-trust property: the cost shown at start is
the cost actually charged).

A turn's cost is three additive factors (α, β, γ all carried in the
snapshot so settle is self-contained):

    cost = α·tokens_cost(model)     # token COGS × per-model markup
         + β·turn_seconds            # global infrastructure occupancy fee
         + γ·model_fee(model)        # per-model value premium, fixed per turn

Everything in this module deals in **µ-credits** (the storage unit;
1 credit = 1_000_000 µ-credits, 1 credit ≈ $0.001 USD).
"""

import math
from datetime import datetime, timezone
from typing import TypedDict

from config import settings

# 1 USD of LLM cost == this many µ-credits, given the peg
# (1 credit = $0.001, 1 credit = 1_000_000 µ-credits → $1 = 1e9 µ-credits).
_USD_TO_MICRO_CREDITS = 1_000_000_000
# 1 whole credit == this many µ-credits (the storage-unit conversion).
_MICRO_PER_CREDIT = 1_000_000


class RateSnapshot(TypedDict):
    """Immutable copy of the pricing inputs needed to settle one turn,
    written verbatim onto the reservation so settle can compute cost
    without re-reading config.

    ``token_markup`` (α) and ``model_fee_credits`` (γ) come from the
    per-model entry in :attr:`config.Settings.credit_rates`;
    ``per_second_credits`` (β) is snapshotted from the global
    :attr:`config.Settings.credit_per_second_credits` at reserve time
    for the same "don't re-price open turns" reason as the token rates.
    """

    model_id: str
    in_per_1k_usd: float
    out_per_1k_usd: float
    token_markup: float
    model_fee_credits: float
    per_second_credits: float
    reserve_max_output_tokens: int
    captured_at: str


def snapshot_rate(model_id: str) -> RateSnapshot:
    """Return a fresh snapshot of the current rate-card row for
    ``model_id``. Raises ``KeyError`` if the model isn't priced —
    that's intentional: silently charging zero for an un-priced model
    is the kind of bug that costs money for months before anyone
    notices.
    """
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
        token_markup=float(card["token_markup"]),
        model_fee_credits=float(card["model_fee_credits"]),
        per_second_credits=float(settings.credit_per_second_credits),
        reserve_max_output_tokens=int(card["reserve_max_output_tokens"]),
        captured_at=datetime.now(timezone.utc).isoformat(),
    )


def _normalize_rate_snapshot(raw: dict) -> RateSnapshot:
    """Coerce a (possibly legacy) JSONB snapshot into a current
    :class:`RateSnapshot`.

    Used at the JSONB → typed boundary in settle, so the rest of the
    code only ever sees the current shape. Legacy reservations written
    before the three-factor refactor carry ``margin`` instead of
    ``token_markup`` and ``max_output_tokens`` instead of
    ``reserve_max_output_tokens``, and don't have the β/γ fields; we
    accept either name on input and default β/γ to 0. Invariant: a
    legacy reservation bills identically to its pre-refactor formula.

    Raises ``KeyError`` if a required field is missing under either
    its current or legacy name — corrupted snapshot, fail loud rather
    than silently substitute a wrong value.
    """
    if "token_markup" in raw:
        token_markup = float(raw["token_markup"])
    elif "margin" in raw:
        token_markup = float(raw["margin"])
    else:
        raise KeyError(
            "rate_snapshot missing both 'token_markup' and legacy "
            f"'margin'; corrupted snapshot: {raw!r}"
        )
    if "reserve_max_output_tokens" in raw:
        reserve_max_output_tokens = int(raw["reserve_max_output_tokens"])
    elif "max_output_tokens" in raw:
        reserve_max_output_tokens = int(raw["max_output_tokens"])
    else:
        raise KeyError(
            "rate_snapshot missing both 'reserve_max_output_tokens' and "
            f"legacy 'max_output_tokens'; corrupted snapshot: {raw!r}"
        )
    return RateSnapshot(
        model_id=raw["model_id"],
        in_per_1k_usd=float(raw["in_per_1k_usd"]),
        out_per_1k_usd=float(raw["out_per_1k_usd"]),
        token_markup=token_markup,
        model_fee_credits=float(raw.get("model_fee_credits", 0.0)),
        per_second_credits=float(raw.get("per_second_credits", 0.0)),
        reserve_max_output_tokens=reserve_max_output_tokens,
        captured_at=raw["captured_at"],
    )


def cost_micro_credits(
    rate: RateSnapshot,
    *,
    input_tokens: int,
    output_tokens: int,
    turn_seconds: float = 0.0,
) -> int:
    """Compute total cost in µ-credits using the three-factor formula.

    Math (all converged on µ-credits in one ``ceil`` at the end so
    rounding happens at the storage boundary, not per term)::

        token_micro    = ((in/1k · in_per_1k_usd
                         + out/1k · out_per_1k_usd) · token_markup)
                       · 1e9                                  # USD → µ-credits
        duration_micro = max(0, turn_seconds) · per_second_credits
                       · 1_000_000                            # credits → µ-credits
        fee_micro      = model_fee_credits · 1_000_000

        cost_micro     = ceil(token_micro + duration_micro + fee_micro)

    ``ceil`` rather than ``round`` so sub-µ-credit fractions land on
    the operator's side — billing-safe rounding.
    """
    token_micro = (
        (input_tokens / 1000.0) * rate["in_per_1k_usd"]
        + (output_tokens / 1000.0) * rate["out_per_1k_usd"]
    ) * rate["token_markup"] * _USD_TO_MICRO_CREDITS
    duration_micro = (
        max(0.0, turn_seconds)
        * rate["per_second_credits"]
        * _MICRO_PER_CREDIT
    )
    fee_micro = rate["model_fee_credits"] * _MICRO_PER_CREDIT
    return int(math.ceil(token_micro + duration_micro + fee_micro))


def reserve_micro_credits(
    rate: RateSnapshot, *, max_input_tokens: int,
) -> int:
    """Compute the upfront reserve amount in µ-credits.

    Pessimistic upper bound across all three factors so the hold
    covers the worst plausible turn. Input length comes from the
    caller (:attr:`config.Settings.credit_reserve_max_input_tokens`),
    output from the rate's own ``reserve_max_output_tokens`` cap,
    duration from :attr:`config.Settings.credit_reserve_max_turn_seconds`.
    Settle multi-refunds whatever wasn't used.
    """
    return cost_micro_credits(
        rate,
        input_tokens=max_input_tokens,
        output_tokens=rate["reserve_max_output_tokens"],
        turn_seconds=float(settings.credit_reserve_max_turn_seconds),
    )


def credits_from_micro(amount_micro: int) -> int:
    """Convert µ-credits to whole-credit integer (display only)."""
    return amount_micro // _MICRO_PER_CREDIT


def micro_from_credits(amount_credits: int) -> int:
    """Convert whole-credit integer to µ-credits (storage)."""
    return amount_credits * _MICRO_PER_CREDIT
