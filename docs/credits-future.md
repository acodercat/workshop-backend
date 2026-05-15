# Credits — deferred work (V1.5 / V2 / V3)

V1 ships the PAYG ledger and the two reservation lifecycle points
(reserve at turn start, settle at `agent.stopped`). It deliberately
leaves four things out. This doc captures *what* and *why* so the
next person picking up the system has the design context.

The storage schema (`credit_ledger`, `credit_reservation`,
`user.credit_balance`) is already wide enough for every item below
— **no migration is required** for any of V1.5 / V2 / V3.

## V1.5 — Reservation janitor

**What.** A periodic asyncio task (or a separate `python -m
app.workers.credits_janitor` process) that scans

```sql
SELECT id, session_id FROM credit_reservation
WHERE state = 'OPEN' AND expires_at < now()
```

and routes each row to `credits.settle_open_for_session` or
`credits.refund_reservation` depending on what's in the Redis
stream tail (mirrors the lazy-sweep logic in
`app/services/credits/settle.py::settle_open_for_user`).

**Why V1 ships without it.** The lazy sweep on user actions (next
`queue_user_message`, next `GET /credits`) covers the common case —
abandoned reservations from worker crashes get cleaned up the next
time the user does anything. The risk window is "worker crashed +
user never returns" → reservation stays open forever → cached
balance under-counts by the reservation amount. Bounded loss
(`<= reserved_amount` per orphan), recoverable by a one-shot rebuild
script.

**When to add.** First evidence of a real long-lived orphan in prod,
or when product launches background-running agent flows where no
user is expected to log back in promptly.

**Implementation outline.** New file
`app/workers/credits_janitor.py`; an `async def run_forever()` loop
on `settings.credit_janitor_interval_seconds` (default 60). Started
either from the FastAPI `lifespan` (single-process) or as a sibling
process under the same deploy script.

## V2 — Worker-side single-turn cost cap

**What.** Inside the agent loop in `axon/src/axon/agent.py`, a
running cost accumulator + hard stop:

```python
turn_cost_micro = 0
for chunk in self.stream():
    if chunk.usage:
        turn_cost_micro += cost_micro_credits(rate_snapshot,
                                              input_tokens=chunk.usage.prompt_tokens,
                                              output_tokens=chunk.usage.completion_tokens)
    if turn_cost_micro >= cost_cap_micro:
        await self.cancel(reason="cost_cap_exceeded")
        break
```

`cost_cap_micro` is passed in at agent construction; backend computes
`min(user_balance * 1.2, ABSOLUTE_MAX_PER_TURN_MICRO)` and forwards
it alongside the existing turn payload. Sits next to the existing
`max_turns` check; same shape, different dimension.

**Why V1 ships without it.** Worker-side changes were explicitly out
of scope (per the design conversation that produced V1). Without the
cap, a single runaway agent loop can blow past the reservation by
up to `(actual usage - reserved)` µ-credits — the user goes
negative, settle posts a real debit larger than the reserve, and
the next reserve attempt fails until they top up. **In practice
the cap is the difference between "user briefly goes -$1" and "user
briefly goes -$50"**; both are recoverable, neither is catastrophic.

**When to add.** First observation of a runaway turn that costs
>10× its reservation, or when the product opens to non-trusted users
where the support cost of one misbehaving agent is real.

**Cap policy.** Two-layer:

- Per-user soft cap: `user.credit_balance * SAFETY_FACTOR`, default
  1.2. Lets users burn through their balance + 20% but no more.
- Absolute hard cap: `ABSOLUTE_MAX_PER_TURN_MICRO`, default ~2000
  credits ($2). Even a rich user can't accidentally drop $50 on one
  turn through a model mis-configuration.

`cost_cap_micro = min(soft, hard)`.

## V2 — Sandbox-seconds billing

**What.** Two new integration points around the sandbox lifecycle:

- At `Sandbox` row create (worker-side, `agent_worker/...` —
  authoritative producer of sandbox rows): call
  `credits.reserve_for_sandbox(user_id, sandbox_id,
  max_runtime_seconds, sandbox_rate_snapshot)`. New kind
  `CreditReservationKind.SANDBOX` (already in the enum).
- At `Sandbox` row terminal-status update: call
  `credits.settle_sandbox(reservation_id, actual_seconds)` —
  mirrors the turn-settle two-row pattern but rate uses
  `per_second_usd` instead of token rates.

**Why V1 ships without it.** Adding two new integration points on
the worker side (not backend) was out of scope. Sandbox runtime cost
is real (gVisor container compute time) but small relative to LLM
tokens for typical workloads. The risk surface is "user idles
sandboxes" — manageable while users are trusted.

**When to add.** First observation of a user racking up sandbox time
without LLM activity, or when the cost of a 24h-idle sandbox starts
showing up in infrastructure bills.

**Rate-card extension.** Add a `"sandbox"` key to
`settings.credit_rates`:

```python
"sandbox": {"per_second_usd": 0.0001, "margin": 1.3}
```

`rates.py` needs a `snapshot_sandbox_rate()` helper alongside the
existing model-id-keyed `snapshot_rate()`.

## V3 — Payment integration

**What.** A webhook endpoint (Stripe / Alipay / WeChat Pay) that
maps `payment_intent.succeeded` events to
`credits.apply_topup(..., reference={source: "alipay",
payment_intent_id: ...})`. The `idempotency_key` is derived from the
payment provider's event id, so webhook retries are safe.

**Why V1 ships without it.** No payment provider contract yet, and
the admin top-up script handles every V1 flow (private beta,
internal testing, manual customer support credits).

**When to add.** When the product opens to paying users.

**Schema impact.** Zero — `TOPUP` ledger rows already carry an
arbitrary `reference` jsonb where the payment metadata lives.
`apply_topup`'s existing `source` reference field documents the
origin.

**New surface.** One webhook endpoint per provider
(`app/api/endpoints/webhooks/`), provider-specific signature
verification (the only security-critical part of the integration).
The endpoint translates a verified webhook into one
`apply_topup` call — that's the whole feature.

## Decisions worth re-visiting in V2 planning

- **Lazy-sweep + reserve transaction coupling.** Currently the
  lazy sweep in `queue_user_message` runs in the same transaction
  as the new reserve. If reserve fails (insufficient credits), the
  sweep also rolls back — the priors stay OPEN. Next attempt finds
  the stream deleted (we cleared it before reserve) and refunds
  instead of settles. Net effect: a failed reserve causes the
  *previous* turn to end up un-billed. Small leak, bounded by
  `priors_settled_amount`. Fix: move the sweep into its own
  transaction before the reserve, accept the worse latency.
- **Settle-from-stream-tail.** `_read_usage_from_stream_tail` reads
  `XREVRANGE COUNT 1`. If the worker emits any post-`agent.stopped`
  event (currently doesn't), we'd grab the wrong row. Worth a
  contract test.
- **Multiple reservations per user.** `settle_open_for_user`
  handles N reservations but does them serially in one
  transaction. If N grows (which it shouldn't, per the partial
  index assumption) the transaction gets long. If a long-tail user
  ever has 100+ open reservations, batch the sweep or split per
  reservation.

## Files and functions referenced

| Concept | Location |
|---|---|
| Service module | `app/services/credits/` |
| Reservation lifecycle states | `app/models/credit_reservation.py::CreditReservationState` |
| Ledger kind enum | `app/models/credit_ledger.py::CreditLedgerKind` |
| Rate card | `config.py::Settings.credit_rates` |
| Reserve integration | `app/services/session/enqueue.py::queue_user_message` |
| Eager settle integration | `app/services/session/streaming.py::stream_events` |
| Lazy sweep | `app/services/credits/settle.py::settle_open_for_user` |
| Admin top-up | `scripts/topup_credits.py` |
| Migration | `alembic/versions/9e3a8b714f02_add_credit_ledger_reservation_balance.py` |
