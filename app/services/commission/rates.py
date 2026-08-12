"""RMB ↔ cents conversion for the commission wallet.

The commission wallet stores **integer RMB cents (整数分)**; the wire
surface (``GET /commissions/*``, withdrawal / convert requests) speaks
**yuan** (``*_cny``) to match the buy-side payment API. Conversion uses
``Decimal`` (not float) so ``¥0.1`` can't drift — mirroring the Decimal
discipline in :mod:`app.services.credits.rates`.
"""

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal


def fen_from_cny(amount_cny: float | str) -> int:
    """Convert a yuan amount to integer RMB cents (分).

    ``ROUND_HALF_UP`` so a clean ¥X.YZ request round-trips exactly; the
    caller validates bounds against the cents value.
    """
    fen = Decimal(str(amount_cny)) * 100
    return int(fen.to_integral_value(rounding=ROUND_HALF_UP))


def cny_from_fen(amount_fen: int) -> float:
    """Convert integer RMB cents (分) to a yuan float for the wire.

    Display-only — two-decimal yuan; callers never do money math on the
    float.
    """
    return float(Decimal(amount_fen) / 100)


def commission_fen(amount_fen: int, rate: float) -> int:
    """Commission (分) earned on a recharge of ``amount_fen`` at ``rate``.

    Floors any sub-cent remainder in the operator's favor (``ROUND_DOWN``),
    matching the buy-side ``credits_from_cny`` floor convention.
    """
    earned = Decimal(amount_fen) * Decimal(str(rate))
    return int(earned.to_integral_value(rounding=ROUND_DOWN))
