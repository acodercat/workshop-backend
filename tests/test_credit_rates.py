import math

import pytest

from app.services.credits import rates


def _rate(**overrides):
    base = {
        "model_id": "test-model",
        "in_per_1k_usd": 0.001,
        "out_per_1k_usd": 0.005,
        "token_markup": 2.0,
        "model_fee_credits": 0.0,
        "per_second_credits": 0.0,
        "reserve_max_output_tokens": 2000,
        "captured_at": "2026-05-20T00:00:00+00:00",
    }
    base.update(overrides)
    return rates.RateSnapshot(**base)


def test_cost_micro_credits_token_only() -> None:
    cost = rates.cost_micro_credits(
        _rate(),
        input_tokens=1000,
        output_tokens=2000,
    )

    assert cost == 22_000_000


def test_cost_micro_credits_uses_three_factor_formula() -> None:
    cost = rates.cost_micro_credits(
        _rate(
            in_per_1k_usd=0.001,
            out_per_1k_usd=0.002,
            token_markup=3.0,
            per_second_credits=0.25,
            model_fee_credits=1.5,
        ),
        input_tokens=1500,
        output_tokens=2500,
        turn_seconds=10.0,
    )

    token_micro = ((1.5 * 0.001) + (2.5 * 0.002)) * 3.0 * rates._USD_TO_MICRO_CREDITS
    duration_micro = 10.0 * 0.25 * rates._MICRO_PER_CREDIT
    fee_micro = 1.5 * rates._MICRO_PER_CREDIT
    assert cost == math.ceil(token_micro + duration_micro + fee_micro)


def test_cost_micro_credits_clamps_negative_turn_seconds() -> None:
    cost = rates.cost_micro_credits(
        _rate(per_second_credits=100.0),
        input_tokens=0,
        output_tokens=0,
        turn_seconds=-30.0,
    )

    assert cost == 0


def test_reserve_micro_credits_covers_tokens_duration_and_fee(monkeypatch) -> None:
    monkeypatch.setattr(rates.settings, "credit_reserve_max_turn_seconds", 120)

    cost = rates.reserve_micro_credits(
        _rate(
            in_per_1k_usd=0.001,
            out_per_1k_usd=0.002,
            token_markup=2.0,
            per_second_credits=0.5,
            model_fee_credits=3.0,
            reserve_max_output_tokens=3000,
        ),
        max_input_tokens=1000,
    )

    token_micro = ((1.0 * 0.001) + (3.0 * 0.002)) * 2.0 * rates._USD_TO_MICRO_CREDITS
    duration_micro = 120 * 0.5 * rates._MICRO_PER_CREDIT
    fee_micro = 3.0 * rates._MICRO_PER_CREDIT
    assert cost == math.ceil(token_micro + duration_micro + fee_micro)


def test_normalize_legacy_snapshot_uses_real_production_shape() -> None:
    """The actual JSONB shape written before the three-factor refactor
    carries BOTH legacy names: ``margin`` (not ``token_markup``) and
    ``max_output_tokens`` (not ``reserve_max_output_tokens``). β/γ
    fields are absent. Confirmed against production DB snapshots.
    """
    snapshot = rates.normalize_rate_snapshot({
        "model_id": "legacy-model",
        "in_per_1k_usd": 0.001,
        "out_per_1k_usd": 0.005,
        "margin": 1.3,
        "max_output_tokens": 4096,
        "captured_at": "2026-05-01T00:00:00+00:00",
    })

    assert snapshot["token_markup"] == 1.3
    assert snapshot["reserve_max_output_tokens"] == 4096
    assert snapshot["model_fee_credits"] == 0.0
    assert snapshot["per_second_credits"] == 0.0


def test_normalize_snapshot_requires_markup_or_legacy_margin() -> None:
    with pytest.raises(KeyError, match="token_markup"):
        rates.normalize_rate_snapshot({
            "model_id": "broken",
            "in_per_1k_usd": 0.001,
            "out_per_1k_usd": 0.005,
            "reserve_max_output_tokens": 4096,
            "captured_at": "2026-05-01T00:00:00+00:00",
        })


def test_normalize_snapshot_requires_reserve_max_output_or_legacy() -> None:
    with pytest.raises(KeyError, match="reserve_max_output_tokens"):
        rates.normalize_rate_snapshot({
            "model_id": "broken",
            "in_per_1k_usd": 0.001,
            "out_per_1k_usd": 0.005,
            "token_markup": 1.3,
            "captured_at": "2026-05-01T00:00:00+00:00",
        })


def test_snapshot_rate_unpriced_model_raises_typed_503() -> None:
    # An unpriced model_id must fail as a typed AppError (→ 503), not a raw
    # KeyError that the catch-all renders as 500.
    from app.errors import ModelUnavailableError

    with pytest.raises(ModelUnavailableError) as exc_info:
        rates.snapshot_rate("model-with-no-rate-card")
    assert exc_info.value.status_code == 503
    assert exc_info.value.error_type == "model_unavailable"


def test_credits_from_cny_uses_sale_price(monkeypatch) -> None:
    monkeypatch.setattr(rates.settings, "credit_price_cny", 0.1)

    assert rates.credits_from_cny(100) == 1000   # ¥100 → 1000 credits
    assert rates.credits_from_cny(50) == 500
    assert rates.credits_from_cny(0.1) == 1
    # Decimal path: naive float ``100 / 0.1`` can be 999.999… and floor
    # to 999 — the customer must not be shortchanged a credit.
    assert rates.credits_from_cny(100.0) == 1000
    # Sub-credit remainder floors in the operator's favor.
    assert rates.credits_from_cny(99.95) == 999


def test_credits_from_cny_respects_configured_price(monkeypatch) -> None:
    monkeypatch.setattr(rates.settings, "credit_price_cny", 0.05)
    assert rates.credits_from_cny(100) == 2000   # ¥0.05/credit → ¥100 = 2000


# ── reserve_est_input_tokens: per-model reserve sizing ───────────────────────


def test_reserve_est_input_tokens_reads_per_model_value(monkeypatch) -> None:
    """A model whose rate-card entry carries ``reserve_est_input_tokens``
    sizes its hold from that per-model figure."""
    monkeypatch.setattr(
        rates.settings, "credit_rates",
        {"m": {"reserve_est_input_tokens": 425_000}},
    )

    assert rates.reserve_est_input_tokens("m") == 425_000


def test_reserve_est_input_tokens_falls_back_to_global(monkeypatch) -> None:
    """Entries without the key (and unknown models) fall back to the global
    ``credit_reserve_max_input_tokens`` — a card edit can't silently zero
    the reserve estimate."""
    monkeypatch.setattr(rates.settings, "credit_rates", {"m": {}})
    monkeypatch.setattr(rates.settings, "credit_reserve_max_input_tokens", 16_000)

    assert rates.reserve_est_input_tokens("m") == 16_000
    assert rates.reserve_est_input_tokens("unknown-model") == 16_000
