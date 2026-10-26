# Credits — deferred work

Current billing is turn-scoped PAYG:

- reserve at turn enqueue;
- worker writes durable usage to
  `session_turn.input_tokens` / `output_tokens` / `terminated_at`;
- worker `PUBLISH`es the session_id on `settle:notify` post-commit
  so the backend settles within ~1 s (push path);
- 60s background sweeper closes anything the push path missed
  (pull path);
- backend settles from `session_turn` and the frozen
  `credit_reservation.rate_snapshot`.

This doc tracks what is still intentionally deferred. It should not
be read as a schema promise: recent billing work added
`session_turn.terminated_at`, added a partial unique index for one
OPEN reservation per session, removed unused
`credit_reservation.kind` / `sandbox_id`, and added the
`settle_subscriber` push path alongside the existing `settle_sweeper`
poll.

## Shipped — Background process

Both `app/workers/settle_sweeper.py` and `settle_subscriber.py` run
together as one sibling process (see `app/workers/run.py`), coordinated
across hosts by a Postgres advisory lock (`b"STLSWEEP"`) on the
sweeper side. The sweeper scans only actionable `OPEN` reservations:

```sql
state = 'OPEN'
AND (
  session_id IS NULL
  OR session.status IS terminal
  OR expires_at < now()
)
```

Routing (same shape for both push and pull):

- `session_id IS NULL` → refund;
- terminal session → `credits.settle_open_for_session`, which reads
  durable usage from `session_turn`;
- expired non-terminal session → refund;
- otherwise leave the reservation open.

The subscriber listens on `settle:notify`; the worker publishes the
session_id immediately after committing its terminal `session_turn`
write, so settle happens within sub-second. Pub/sub is
fire-and-forget — a lost message is caught by the next sweeper tick.

Billing source of truth is Postgres; Redis SSE streams remain UI
transport only.

### Open hazard — fixed timeout vs. long turns

The sweeper's dead-worker signal is still fixed-time based:
`expires_at < now() AND session.status non-terminal`. A legitimate
turn that runs past `expires_at` is indistinguishable from a dead
worker, so it can be refunded mid-flight. When the worker later
finishes, settlement sees a non-OPEN reservation and no-ops; the user
gets that turn for free.

Current mitigation: `reserve.py` uses a conservative 4h expiry. This
widens the window but does not close it.

Real fix: add a worker-produced liveness signal for billing cleanup,
for example `session_turn.last_heartbeat_at` or a side-table row
updated while the turn is in flight. The sweeper refund predicate
then becomes heartbeat-staleness based instead of fixed reservation
expiry.

## Next — Worker-side single-turn cost cap

**What.** Pass a `cost_cap_micro` into the worker/agent turn and stop
the turn once estimated runtime cost reaches the cap. The accumulator
should use the same frozen rate snapshot as settlement:

```python
turn_cost_micro = 0
started_at = monotonic()
for chunk in self.stream():
    if chunk.usage:
        turn_cost_micro = cost_micro_credits(
            rate_snapshot,
            input_tokens=chunk.usage.prompt_tokens,
            output_tokens=chunk.usage.completion_tokens,
            turn_seconds=monotonic() - started_at,
        )
    if turn_cost_micro >= cost_cap_micro:
        await self.cancel(reason="cost_cap_exceeded")
        break
```

**Why it is deferred.** Without the cap, a runaway turn can cost more
than the upfront reserve and push the user negative. That is
recoverable but creates support and trust risk once usage is no longer
limited to trusted/internal users.

**Suggested policy.**

- Soft cap: `user.credit_balance * SAFETY_FACTOR`, default `1.2`.
- Hard cap: `ABSOLUTE_MAX_PER_TURN_MICRO`, product-defined.
- `cost_cap_micro = min(soft, hard)`.

## Decisions Worth Revisiting

- **Long lazy sweeps.** `settle_open_for_user` handles multiple open
  reservations serially in one transaction. Typical N is 0 or 1. If
  a long-tail user ever has hundreds of open reservations, batch or
  split per reservation.
- **Org-level billing.** Billing still attaches to `user_id`.
  Moving to organization-level balances is a separate migration and
  should be driven by product packaging, not hidden inside turn
  billing.
- **Queue time is billed as occupancy.** The β duration fee is
  `terminated_at − session_turn.created_at`, and `created_at` is the
  enqueue INSERT, so a turn that waits PENDING behind a saturated fleet
  pays `per_second_credits` for time no sandbox ran (10 minutes queued ≈
  6 credits at the default β); the budget enforcer projects the same
  span. The fix is a `session_turn.claimed_at` the worker stamps in its
  claim UPDATE, with settle and the enforcer measuring from
  `coalesce(claimed_at, created_at)`. Deferred because `core/agent-worker`
  is shared: its claim SQL can name the new column only once every
  database it serves has the migration, so the order is migrate all of
  them, then deploy the worker.
- **Sandbox runtime cost.** The old reserved `SANDBOX` variant was
  removed. Current direction is turn-level three-factor billing
  (`tokens + turn_seconds + model_fee`). Reintroduce separate sandbox
  billing only if infrastructure cost data proves turn-level duration
  is too coarse.

## Reference

| Concept | Location |
|---|---|
| Credit services | `app/services/credits/` |
| Rate card and cost math | `app/services/credits/rates.py` |
| Reserve integration | `app/services/session/enqueue.py::queue_user_message` |
| Lazy sweep | `app/services/credits/settle.py::settle_open_for_user` |
| Sweeper (poll) | `app/workers/settle_sweeper.py` |
| Subscriber (push) | `app/workers/settle_subscriber.py` |
| Admin top-up | `scripts/topup_credits.py` |
