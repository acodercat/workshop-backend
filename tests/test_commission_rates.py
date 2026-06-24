"""Offline unit tests for the commission wallet's RMB-cents arithmetic —
pure functions, no DB/network."""

from app.services.commission.rates import commission_fen, cny_from_fen, fen_from_cny


def test_commission_fen_is_rate_times_amount() -> None:
    # ¥100 recharge (10000 分) at 10% → ¥10 (1000 分).
    assert commission_fen(10000, 0.1) == 1000
    assert commission_fen(5000, 0.1) == 500


def test_commission_fen_floors_sub_cent_in_operator_favor() -> None:
    # 333 分 @ 10% = 33.3 分 → floors to 33.
    assert commission_fen(333, 0.1) == 33
    assert commission_fen(10000, 0.0) == 0


def test_fen_cny_round_trip() -> None:
    assert fen_from_cny(100) == 10000
    assert fen_from_cny(0.1) == 10
    assert fen_from_cny(12.34) == 1234
    assert cny_from_fen(10000) == 100.0
    assert cny_from_fen(1234) == 12.34
