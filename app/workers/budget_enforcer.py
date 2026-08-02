"""Stop in-flight turns that are outrunning their user's balance.

The reserve hold is an entry gate sized to the model's *median* turn
(``reserve_est_input_tokens``), not a worst-case bound — agent-turn costs
span ~350×, so any upfront hold either blocks cheap turns or under-covers
expensive ones. This enforcer is the mid-turn half of the control, the
OCS-style interim charging loop:

- The worker's lease heartbeat reports each running turn's cumulative
  usage onto ``session_turn.interim_input_tokens`` / ``interim_output_tokens``.
- Every tick prices those reports with the reservation's **frozen**
  ``rate_snapshot`` (same :func:`cost_micro_credits` settle uses — one
  pricing truth) and projects the user's post-settle balance.
- When the projection drops to the buffer, the worst-overrunning turn is
  asked to stop via the existing cancel channel. The worker unwinds
  exactly like a user-initiated cancel: partial output persisted,
  ``CANCELLED`` terminal with actual usage, normal settle.

Money stays entirely backend-side — the worker reports token counts and
obeys cancels; it never sees rates or balances. No ledger writes happen
here: the ledger keeps its three rows per turn (RESERVE + SETTLE×2).

**Projection, not per-turn checks.** A user's concurrent turns all draw on
one balance, so solvency is judged per user: each hold already paid for
``reserved`` of its turn, the true remaining exposure is
``Σ (priced − reserved)`` against the live balance. Stopping is gradual —
one turn per user per tick, worst overrun first — because ending the
biggest spender usually restores solvency for the rest; the next tick
re-evaluates with fresh reports.

Overshoot is bounded by the detection window (heartbeat cadence + tick
interval ≈ tens of seconds of burn — the Manus-style "one action" overage)
instead of a whole turn.

Same multi-node posture as the settle sweeper: a transaction-scoped
advisory lock elects one active poller per tick. **Fail-open by design** —
with this process down, behavior degrades to pre-enforcer semantics: the
turn runs to completion, settle bills actuals, and the reserve gate blocks
the user's next turn. Cancel publishes are likewise best-effort; a missed
one costs one tick of extra burn, never a wrong charge.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import NamedTuple

import redis.asyncio as aioredis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.credit_reservation import CreditReservation, CreditReservationState
from app.models.session import Session, SessionStatus
from app.models.session_turn import SessionTurn, TerminationReason
from app.models.user import User
from app.services.credits.rates import (
    cost_micro_credits,
    micro_from_credits,
    normalize_rate_snapshot,
)
from app.services.redis import cancel_channel
from config import settings

logger = logging.getLogger(__name__)

# 64-bit advisory-lock key, bytes ``b"BUDGETNF"`` — same convention as the
# settle sweeper's b"STLSWEEP", easy to spot in ``pg_locks``.
_LOCK_KEY = 0x4255444745544E46


class RunningTurn(NamedTuple):
    """One in-flight turn priced at its latest interim report."""

    session_id: uuid.UUID
    reserved: int
    priced: int

    @property
    def overrun(self) -> int:
        """µ-credits this turn has consumed beyond its hold (signed)."""
        return self.priced - self.reserved


def select_turn_to_stop(
    turns: list[RunningTurn], *, balance_micro: int, buffer_micro: int,
) -> RunningTurn | None:
    """Pick the turn to stop for one user, or ``None`` to leave all running.

    ``projected`` is the balance after settling every running turn at its
    current cost — the holds were already debited from ``balance_micro``,
    so only the *overruns* beyond them remain to be absorbed. While the
    projection stays above the buffer, nothing stops: a legitimately
    expensive turn a wealthy user can afford is never touched.

    When it doesn't, stop the worst overrunner — unless no turn has a
    positive overrun (a user already insolvent from a prior overdraft
    whose running turns are all inside their holds: those are pre-paid,
    stopping one recovers nothing, and the reserve gate is what blocks
    new turns).
    """
    projected = balance_micro - sum(t.overrun for t in turns)
    if projected > buffer_micro:
        return None
    worst = max(turns, key=lambda t: t.overrun)
    if worst.overrun <= 0:
        return None
    return worst


async def run_forever(
    redis: aioredis.Redis, *, interval_seconds: int | None = None,
) -> None:
    """Periodic enforcement loop. Cancel-safe via :class:`asyncio.CancelledError`.

    Mirrors the settle sweeper's loop shape: per-tick try/except with
    capped exponential backoff so a broken dependency never busy-loops.
    """
    interval = interval_seconds or settings.budget_enforcer_interval_seconds
    consecutive_failures = 0
    while True:
        try:
            stopped = await _tick(redis)
            consecutive_failures = 0
            if stopped:
                logger.info("budget enforcer tick: stopped=%d", stopped)
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            logger.exception(
                "budget enforcer tick failed (consecutive_failures=%d)",
                consecutive_failures,
            )
        delay = (
            min(interval * (2 ** consecutive_failures), 600)
            if consecutive_failures
            else interval
        )
        await asyncio.sleep(delay)


async def _tick(redis: aioredis.Redis) -> int:
    """One enforcement cycle. Returns the number of stop requests sent
    (0 when another process held the advisory lock).

    Read-only against Postgres — the only side effect is the cancel
    publish, so a single session serves the whole tick (no per-row
    transaction isolation needed, unlike the sweeper's settle writes).
    """
    async with AsyncSessionLocal() as db:
        got = (
            await db.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _LOCK_KEY}
            )
        ).scalar()
        if not got:
            return 0

        turns_by_user = await _priced_turns_by_user(db)
        if not turns_by_user:
            return 0
        balances = dict(
            (
                await db.execute(
                    select(User.id, User.credit_balance).where(
                        User.id.in_(turns_by_user.keys())
                    )
                )
            ).all()
        )

    buffer_micro = micro_from_credits(settings.budget_enforcer_buffer_credits)
    stopped = 0
    for user_id, turns in turns_by_user.items():
        pick = select_turn_to_stop(
            turns,
            balance_micro=balances.get(user_id, 0),
            buffer_micro=buffer_micro,
        )
        if pick is None:
            continue
        # Best-effort, like every cancel publish (see the user cancel
        # endpoint): a lost message costs one tick of extra burn and the
        # next tick re-sends. Never fails the whole tick. The payload is
        # the termination reason — the worker stamps it on the turn and
        # the wire terminal so the UI can say "stopped: out of credits".
        try:
            await redis.publish(
                cancel_channel(str(pick.session_id)),
                TerminationReason.INSUFFICIENT_CREDITS.value,
            )
        except Exception:
            logger.exception(
                "budget enforcer: cancel publish failed for session %s",
                pick.session_id,
            )
            continue
        stopped += 1
        logger.warning(
            "budget exhausted: stopping session %s (user=%s priced=%d "
            "reserved=%d balance=%d)",
            pick.session_id, user_id, pick.priced, pick.reserved,
            balances.get(user_id, 0),
        )
    return stopped


async def _priced_turns_by_user(
    db: AsyncSession,
) -> dict[int, list[RunningTurn]]:
    """Every reported in-flight turn, priced at its latest interim usage.

    OPEN reservation × RUNNING session narrows to genuinely in-flight
    turns; ``interim_input_tokens IS NOT NULL`` skips turns that haven't
    heartbeated a report yet (claimed seconds ago — nothing meaningful to
    price). ``turn_seconds`` uses now − created_at, the same span settle
    bills via ``terminated_at``.
    """
    rows = (
        await db.execute(
            select(
                CreditReservation.user_id,
                CreditReservation.session_id,
                CreditReservation.reserved,
                CreditReservation.rate_snapshot,
                SessionTurn.interim_input_tokens,
                SessionTurn.interim_output_tokens,
                SessionTurn.created_at,
            )
            .join(SessionTurn, SessionTurn.id == CreditReservation.turn_id)
            .join(Session, Session.id == CreditReservation.session_id)
            .where(
                CreditReservation.state == CreditReservationState.OPEN,
                Session.status == SessionStatus.RUNNING,
                # Only turns that have reported a heartbeat. The worker
                # writes both interim columns together, so in practice
                # input-not-null implies output-not-null — but the columns
                # are independently nullable, and a single NULL output
                # would make ``cost_micro_credits`` do ``None / 1000`` and
                # crash the whole tick (fleet-wide enforcement pause). The
                # symmetric filter makes that structurally impossible.
                SessionTurn.interim_input_tokens.is_not(None),
                SessionTurn.interim_output_tokens.is_not(None),
            )
        )
    ).all()

    now = datetime.now(timezone.utc)
    turns_by_user: dict[int, list[RunningTurn]] = {}
    for row in rows:
        priced = cost_micro_credits(
            normalize_rate_snapshot(row.rate_snapshot),
            input_tokens=row.interim_input_tokens,
            output_tokens=row.interim_output_tokens,
            turn_seconds=(now - row.created_at).total_seconds(),
        )
        turns_by_user.setdefault(row.user_id, []).append(
            RunningTurn(
                session_id=row.session_id,
                reserved=row.reserved,
                priced=priced,
            )
        )
    return turns_by_user
