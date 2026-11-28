"""One-off: merge the case-duplicate email accounts axon_88 -> axon_112.

Two rows held the same address in different case (``AAAZUO666@163.com`` on
id 88, created before email normalization landed; ``aaazuo666@163.com`` on
id 112). Postgres' ``UNIQUE(email)`` is case-sensitive, so the second
registration was allowed and id 88 became unreachable: every lookup
lowercases its input, and id 88 has no other sign-in channel.

This is option B of the repair — move id 88's whole credit balance onto the
live account, then clear the address on 88 and revoke its sessions. Sessions
and files stay on 88 (invisible to the user, harmless).

Run once from ``backend/``::

    uv run python -m scripts.merge_88_112

Safe to re-run: both adjustments are idempotent on their keys, so a second
run raises ``AdjustmentAlreadyApplied`` and moves nothing.

Preconditions verified before writing this (re-check if time has passed):
id 88's 16 credit_reservations are all SETTLED (no OPEN hold whose settle
would post against a zeroed balance), it has no RUNNING session, it referred
nobody, and ``credit_balance == SUM(credit_ledger.amount)`` held on both rows.
"""

import asyncio

from sqlalchemy import func, select, text, update

from app import models
from app.database import AsyncSessionLocal
from app.services import credits

LOSER, WINNER = 88, 112
KEY = "merge-user-88-into-112-2026-08-31"
REASON = "合并大小写重复邮箱账号 axon_88 -> axon_112"


async def _invariant(db, uid: int) -> tuple[int, int]:
    """``(stored balance, ledger sum)`` — the double-entry pair that must match."""
    balance = await db.scalar(text('SELECT credit_balance FROM "user" WHERE id=:u'), {"u": uid})
    ledger = await db.scalar(
        text("SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE user_id=:u"),
        {"u": uid},
    )
    return balance, ledger


async def main() -> None:
    async with AsyncSessionLocal() as db:
        # ``FOR UPDATE`` so the balance we move is the balance we read. id 112
        # is an active account whose balance moves between runs; id 88 is
        # dormant, but reading the amount under the lock is what makes that a
        # fact rather than an assumption.
        loser = await db.scalar(
            select(models.User).where(models.User.id == LOSER).with_for_update()
        )
        winner = await db.scalar(select(models.User).where(models.User.id == WINNER))
        if loser is None or winner is None:
            raise SystemExit("missing user")

        amount = loser.credit_balance
        print(f"before  {LOSER}: email={loser.email!r} balance={amount}")
        print(f"before {WINNER}: email={winner.email!r} balance={winner.credit_balance}")
        print(f"moving    : {amount} micro ( = {amount / 10_000_000:,.2f} credits )")

        if amount > 0:
            # Never a raw ``credit_ledger`` insert — ``apply_adjust`` is the
            # funnel that keeps ``user.credit_balance == SUM(ledger.amount)``.
            # A duplicate key makes it roll the whole transaction back itself,
            # which is the all-or-nothing outcome we want here.
            await credits.apply_adjust(
                db,
                user_id=LOSER,
                amount_micro=-amount,
                idempotency_key=f"{KEY}-debit",
                reason=REASON,
            )
            await credits.apply_adjust(
                db,
                user_id=WINNER,
                amount_micro=amount,
                idempotency_key=f"{KEY}-credit",
                reason=REASON,
            )
        else:
            print("balance already zero — no ledger movement")

        # Clear the dead row's address, so nothing contacts it and the
        # collision check below no longer counts it, and sign it out by
        # revoking its sessions (app/services/identity/sessions.py).
        loser.email = None
        await db.execute(
            update(models.IdentitySession)
            .where(
                models.IdentitySession.user_id == LOSER,
                models.IdentitySession.revoked_at.is_(None),
            )
            .values(revoked_at=func.now())
        )

        await db.commit()

    # Fresh session, so the checks read committed state rather than identity-map
    # leftovers from the transaction above.
    async with AsyncSessionLocal() as db:
        for uid in (LOSER, WINNER):
            balance, ledger = await _invariant(db, uid)
            verdict = "OK" if balance == ledger else "MISMATCH"
            print(f"after  {uid:3}: balance={balance:>15} ledger_sum={ledger:>15} {verdict}")

        rows = (
            await db.execute(
                text('SELECT id, email FROM "user" WHERE id IN (:a, :b) ORDER BY id'),
                {"a": LOSER, "b": WINNER},
            )
        ).all()
        for row in rows:
            print(f"after  {row.id:3}: email={row.email!r}")

        groups = await db.scalar(
            text(
                """
                SELECT count(*) FROM (
                    SELECT lower(email) FROM "user" WHERE email IS NOT NULL
                    GROUP BY lower(email) HAVING count(*) > 1
                ) collisions
                """
            )
        )
        unnormalized = await db.scalar(
            text('SELECT count(*) FROM "user" WHERE email IS NOT NULL AND email <> lower(email)')
        )
        print(f"remaining case-collision groups : {groups}")
        print(f"remaining un-normalized emails  : {unnormalized}")


if __name__ == "__main__":
    asyncio.run(main())
