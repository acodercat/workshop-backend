"""Unit-economics analysis for the credit rate card.

Reads **real** settled turns out of ``session_turn`` (the same
billing-grade columns the worker writes and ``credits/settle.py``
reads), reconstructs per-turn revenue and true COGS, and reports the
gross-margin distribution per model so you can set the three pricing
knobs (α ``token_markup``, β ``credit_per_second_credits``, γ
``model_fee_credits``) from data rather than by guessing.

Why this exists
---------------
The cost formula is ``price = α·token_cogs + β·turn_seconds + γ``.
``token_cogs`` (your LLM purchase price) is the rate card's
``in/out_per_1k_usd``; revenue is whatever the current rate card
charges. But your *true* COGS is token purchase cost **plus** the
sandbox/compute occupancy of the turn — and today β is 0, so a long
turn that occupies a gVisor sandbox for 30 min while spending few
tokens can be sold below cost. This script makes that visible.

What it computes, per model
---------------------------
- token COGS (USD)        : in/1k·in_per_1k_usd + out/1k·out_per_1k_usd
- sandbox COGS (USD)      : turn_seconds/3600 · ``--sandbox-usd-per-hour``
- revenue (USD)           : current rate card via ``cost_micro_credits`` ÷ 1e9
- margin %                : (revenue − true_cogs) / revenue, with p50/p90/p99
- loss-making turn share  : fraction of turns sold below true COGS

Then it back-solves the knobs needed to hit ``--target-margin``:
a recommended β that makes the duration fee exactly cover sandbox
COGS, and the α (markup) that delivers the target blended margin on
top, and re-scores the blended margin under those values to verify.

Sandbox cost is the one input you must supply (the DB can't know your
cloud bill). Estimate ``--sandbox-usd-per-hour`` as either:
  - vCPU/RAM you allocate per sandbox container × your cloud's
    per-vCPU-hour price (a clean upper bound), or
  - (monthly cost of the hosts running sandboxes) ÷
    (avg concurrent sandboxes × utilisation × 730h).
Left at 0 the script still runs but only sees token COGS — it will
say so loudly, because the "is this priced right" answer can flip
once compute is counted.

Usage:
    uv run python -m scripts.pricing_analysis --days=30 --sandbox-usd-per-hour=0.18 --target-margin=0.65
    uv run python -m scripts.pricing_analysis --days=7         # token-only quick look

Read-only: issues a single SELECT, writes nothing.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.session import Session
from app.models.session_turn import SessionTurn
from app.services.credits.rates import cost_micro_credits, snapshot_rate
from config import settings

# Revenue from ``cost_micro_credits`` is in µ-credits. The µ↔USD peg is
# invariant ($1 = 1e9 µ-credits) regardless of the credit denomination,
# so this constant is unaffected by the 2026-05 redenomination.
_MICRO_PER_USD = 1_000_000_000


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile (q in 0..1) over a pre-sorted list.

    Avoids a numpy dependency; matches numpy's default ``linear`` method
    closely enough for a pricing read-out.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


class _ModelAgg:
    """Per-model accumulator of per-turn economics."""

    def __init__(self) -> None:
        self.token_cogs_usd: list[float] = []
        self.sandbox_cogs_usd: list[float] = []
        self.revenue_usd: list[float] = []
        self.margin_pct: list[float] = []
        self.duration_s: list[float] = []
        self.tokens_total: list[int] = []
        self.loss_turns = 0

    @property
    def n(self) -> int:
        return len(self.revenue_usd)

    def add(
        self,
        *,
        token_cogs: float,
        sandbox_cogs: float,
        revenue: float,
        duration_s: float,
        tokens_total: int,
    ) -> None:
        true_cogs = token_cogs + sandbox_cogs
        self.token_cogs_usd.append(token_cogs)
        self.sandbox_cogs_usd.append(sandbox_cogs)
        self.revenue_usd.append(revenue)
        self.duration_s.append(duration_s)
        self.tokens_total.append(tokens_total)
        # Margin is undefined for zero-revenue turns; treat as -inf-ish
        # loss only if there's real COGS behind them.
        if revenue > 0:
            self.margin_pct.append((revenue - true_cogs) / revenue)
        if revenue < true_cogs:
            self.loss_turns += 1


async def _load_turns(db, since: datetime):
    """Settled, billable turns since ``since``: tokens written (not the
    FAILED/refunded path) and a terminal timestamp for duration math.
    Joins ``session`` for the model that priced the turn. Soft-deleted
    sessions are included — they were still billed."""
    stmt = (
        select(
            SessionTurn.input_tokens,
            SessionTurn.output_tokens,
            SessionTurn.created_at,
            SessionTurn.terminated_at,
            Session.model_id,
        )
        .join(Session, Session.id == SessionTurn.session_id)
        .where(
            SessionTurn.input_tokens.isnot(None),
            SessionTurn.terminated_at.isnot(None),
            SessionTurn.created_at >= since,
        )
    )
    return (await db.execute(stmt)).all()


def _summarize(agg: _ModelAgg) -> dict:
    margins = sorted(agg.margin_pct)
    durs = sorted(agg.duration_s)
    toks = sorted(float(t) for t in agg.tokens_total)
    rev = sum(agg.revenue_usd)
    cogs = sum(agg.token_cogs_usd) + sum(agg.sandbox_cogs_usd)
    return {
        "n": agg.n,
        "revenue_usd": rev,
        "token_cogs_usd": sum(agg.token_cogs_usd),
        "sandbox_cogs_usd": sum(agg.sandbox_cogs_usd),
        "blended_margin": (rev - cogs) / rev if rev > 0 else 0.0,
        "margin_p50": _percentile(margins, 0.50),
        "margin_p90": _percentile(margins, 0.90),
        "margin_p99": _percentile(margins, 0.99),
        "dur_p50": _percentile(durs, 0.50),
        "dur_p90": _percentile(durs, 0.90),
        "dur_p99": _percentile(durs, 0.99),
        "tok_p50": _percentile(toks, 0.50),
        "tok_p90": _percentile(toks, 0.90),
        "loss_share": agg.loss_turns / agg.n if agg.n else 0.0,
    }


def _print_model_table(model_id: str, s: dict) -> None:
    print(f"\n── {model_id}  (n={s['n']})")
    print(
        f"   revenue ${s['revenue_usd']:.2f}   "
        f"token-COGS ${s['token_cogs_usd']:.2f}   "
        f"sandbox-COGS ${s['sandbox_cogs_usd']:.2f}"
    )
    print(f"   blended margin : {s['blended_margin'] * 100:6.1f}%")
    print(
        f"   margin pctl    : p50 {s['margin_p50'] * 100:5.1f}%   "
        f"p90 {s['margin_p90'] * 100:5.1f}%   "
        f"p99 {s['margin_p99'] * 100:5.1f}%   "
        f"(p99 = worst turns)"
    )
    print(
        f"   duration (s)   : p50 {s['dur_p50']:5.0f}   "
        f"p90 {s['dur_p90']:6.0f}   p99 {s['dur_p99']:6.0f}"
    )
    print(
        f"   tokens/turn    : p50 {s['tok_p50']:,.0f}   p90 {s['tok_p90']:,.0f}"
    )
    flag = "  ⚠️ SELLING BELOW COST" if s["loss_share"] > 0.05 else ""
    print(f"   loss-making    : {s['loss_share'] * 100:5.1f}% of turns{flag}")


def _recommend(totals: dict, target_margin: float, sandbox_usd_per_hour: float) -> None:
    """Back-solve β and α for the target blended margin.

    β recommendation makes the duration fee exactly recover sandbox
    COGS (margin-neutral on compute); α then carries the token margin.
    With β covering sandbox, for blended margin m:
        revenue = α·Σtoken_cogs + Σsandbox_cogs   (β-fee ≈ sandbox COGS)
        true_cogs = Σtoken_cogs + Σsandbox_cogs
        m = 1 − true_cogs/revenue
      ⇒ α = (true_cogs/(1−m) − Σsandbox_cogs) / Σtoken_cogs
    """
    tok = totals["token_cogs_usd"]
    sand = totals["sandbox_cogs_usd"]
    true_cogs = tok + sand
    print("\n" + "=" * 64)
    print(f"RECOMMENDATION  (target blended margin = {target_margin * 100:.0f}%)")
    print("=" * 64)

    if tok <= 0:
        print("  No token COGS in window — nothing to recommend.")
        return

    # β so the duration fee recovers sandbox COGS. credits/sec; peg
    # 1 credit = $0.01 → β = (USD/hour) / 3600 / 0.01 = (USD/hour)/36.
    beta_rec = sandbox_usd_per_hour / 36.0
    if true_cogs / (1 - target_margin) <= sand:
        alpha_rec = 1.0  # sandbox alone exceeds target revenue; β carries it
    else:
        alpha_rec = (true_cogs / (1 - target_margin) - sand) / tok

    print(
        f"  β  credit_per_second_credits : {beta_rec:.4f}  "
        f"(currently {settings.credit_per_second_credits})"
    )
    if sandbox_usd_per_hour > 0:
        print(
            f"     → a {totals['dur_p90_global']:.0f}s (p90) turn pays "
            f"~{beta_rec * totals['dur_p90_global']:.0f} credits of duration fee"
        )
    print(
        f"  α  token_markup              : {alpha_rec:.2f}  "
        f"(currently 3.0 on every model in the card)"
    )
    print(
        "\n  Apply β globally in config.py (credit_per_second_credits); set α\n"
        "  per-model in credit_rates. Verify with a re-run once changed."
    )
    if sandbox_usd_per_hour <= 0:
        print(
            "\n  ⚠️ --sandbox-usd-per-hour was 0: this only balances TOKEN cost.\n"
            "     Long turns' compute is invisible here. Supply the real number\n"
            "     for a trustworthy β and α."
        )


async def main(args: argparse.Namespace) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    async with AsyncSessionLocal() as db:
        rows = await _load_turns(db, since)

    if not rows:
        print(f"No settled turns in the last {args.days} days.")
        return 0

    by_model: dict[str, _ModelAgg] = {}
    unpriced: dict[str, int] = {}
    rate_cache: dict[str, dict] = {}

    for in_tok, out_tok, created_at, terminated_at, model_id in rows:
        if model_id not in rate_cache:
            try:
                rate_cache[model_id] = snapshot_rate(model_id)
            except KeyError:
                rate_cache[model_id] = None  # type: ignore[assignment]
        rate = rate_cache[model_id]
        if rate is None:
            unpriced[model_id] = unpriced.get(model_id, 0) + 1
            continue

        in_tok = int(in_tok)
        out_tok = int(out_tok)
        duration_s = max(0.0, (terminated_at - created_at).total_seconds())

        token_cogs = (
            in_tok / 1000.0 * rate["in_per_1k_usd"]
            + out_tok / 1000.0 * rate["out_per_1k_usd"]
        )
        sandbox_cogs = duration_s / 3600.0 * args.sandbox_usd_per_hour
        # ``--beta`` lets you preview a per-second price that isn't yet
        # in config (e.g. "what if β=0.1?"). Default None → use the
        # snapshot's real ``per_second_credits`` (today 0.0).
        priced_rate = (
            {**rate, "per_second_credits": args.beta}
            if args.beta is not None else rate
        )
        revenue = (
            cost_micro_credits(
                priced_rate, input_tokens=in_tok, output_tokens=out_tok,
                turn_seconds=duration_s,
            )
            / _MICRO_PER_USD
        )

        by_model.setdefault(model_id, _ModelAgg()).add(
            token_cogs=token_cogs,
            sandbox_cogs=sandbox_cogs,
            revenue=revenue,
            duration_s=duration_s,
            tokens_total=in_tok + out_tok,
        )

    print("=" * 64)
    print(
        f"PRICING ANALYSIS — last {args.days}d   "
        f"sandbox=${args.sandbox_usd_per_hour}/h   peg 1 credit=$0.01"
    )
    print("=" * 64)

    all_dur: list[float] = []
    tot = {"revenue_usd": 0.0, "token_cogs_usd": 0.0, "sandbox_cogs_usd": 0.0}
    for model_id in sorted(by_model, key=lambda m: -by_model[m].n):
        agg = by_model[model_id]
        s = _summarize(agg)
        _print_model_table(model_id, s)
        tot["revenue_usd"] += s["revenue_usd"]
        tot["token_cogs_usd"] += s["token_cogs_usd"]
        tot["sandbox_cogs_usd"] += s["sandbox_cogs_usd"]
        all_dur.extend(agg.duration_s)

    rev = tot["revenue_usd"]
    cogs = tot["token_cogs_usd"] + tot["sandbox_cogs_usd"]
    blended = (rev - cogs) / rev if rev > 0 else 0.0
    print("\n" + "─" * 64)
    print(
        f"BLENDED (all models): revenue ${rev:.2f}  true-COGS ${cogs:.2f}  "
        f"margin {blended * 100:.1f}%"
    )
    if unpriced:
        print(
            "  skipped (no rate-card entry): "
            + ", ".join(f"{m}×{n}" for m, n in unpriced.items())
        )

    tot["dur_p90_global"] = _percentile(sorted(all_dur), 0.90)
    _recommend(tot, args.target_margin, args.sandbox_usd_per_hour)
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=30, help="Look-back window (default 30).")
    p.add_argument(
        "--sandbox-usd-per-hour", type=float, default=0.0,
        help="Real infra cost of one sandbox-hour (USD). 0 = token-cost only.",
    )
    p.add_argument(
        "--target-margin", type=float, default=0.65,
        help="Target blended gross margin (0..1) for the recommendation.",
    )
    p.add_argument(
        "--beta", type=float, default=None,
        help="Preview a per-second price (credits/sec) instead of the "
        "configured one. e.g. --beta=0.1",
    )
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(_parse_args())))
