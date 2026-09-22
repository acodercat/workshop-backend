# Credits & Billing — Architecture and Operations

This is the single-source design doc for the credit / quota system.
Code lives under `app/services/credits/`, `app/workers/settle_sweeper.py`
+ `app/workers/settle_subscriber.py`, and the relevant alembic
migrations. Deferred work and forward-looking plans live in
[`credits-future.md`](credits-future.md).

---

## 1. TL;DR

- **PAYG, double-entry ledger** with reserve-then-settle semantics.
- **All math is integer µ-credits** (1 credit = 10,000,000 µ-credits,
  1 credit ≈ \$0.01 USD of LLM cost). No floats anywhere downstream
  of `app/services/credits/rates.py`.
- **Pre-deduct + settle** — the right pattern for an agent platform
  where a single turn can cost \$1–\$5 and run for minutes; not the
  post-debit pattern that high-QPS chat APIs use (LiteLLM-style).
- **Worker doesn't know about credits.** It only writes
  `session_turn.input_tokens` / `output_tokens` in its existing
  fenced terminal transaction; the backend reads from there.
- **Three settlement triggers** keep reservations from leaking:
  - lazy sweep on every user-facing endpoint hit (active user);
  - Redis `settle:notify` subscriber for sub-second latency on
    completed turns (`settle_subscriber`);
  - 60s background sweeper for the "user never comes back" case
    (`settle_sweeper`).
- **Multi-node ready** via Postgres advisory locks — only one
  sweeper polls per tick across the cluster; subscribers serialize
  via the per-reservation `FOR UPDATE` lock.
- **Every user has an Organization** (mirrors OpenAI). Personal org
  is auto-created at registration; B2B teams layer on top later
  without schema migration.

---

## 2. Why this shape (design principles)

Every choice below was made with three pressures in mind:

1. **Agent workload is high-stakes per call.** A runaway agent loop
   can spend \$5+ before it stops. We need the ability to **refuse
   service** when a user can't afford the worst-case cost.
2. **Audit trail is non-negotiable.** First customer to ask "where
   did my 10,000 credits go" needs a per-row answer, not a balance
   delta.
3. **The system has to keep working when half the things go wrong**
   — worker crashes, user abandons mid-turn, DB blip during commit,
   Redis stream gets deleted before settlement.

### Double-entry ledger, not balance arithmetic

We could store one `user.credit_balance` column and just `UPDATE …
SET credit_balance = credit_balance - X`. We chose not to.

```
Every credit movement = one immutable row in ``credit_ledger``.
Balance = SUM(ledger.amount). The column is a cache.
```

This costs us ~2× the write volume but buys:

- **Auditability**: every charge / refund / top-up is its own row
  with a `reference` JSONB explaining what it was for.
- **Replay-ability**: the cached balance can be rebuilt from the
  ledger at any time (`balance == SUM(amount) WHERE user_id = X`).
  This is what we run as the nightly invariant check.
- **Pricing-change safety**: each settle records the *rate snapshot
  used*, not a delta against current rates. Yesterday's bill won't
  change when today's prices do.

Industry references that converge on the same shape: Stripe (their
internal balance system), m3ter / Lago / Orb (modern usage-based
billing platforms), OneAPI (LLM proxy quota system).

### Integer µ-credits in `bigint`

Floats accumulate drift over millions of rows. `numeric` is slow and
its `SUM` is provider-dependent. We use `bigint` with a fixed scale:

```
1 USD             = 1_000_000_000 µ-credits        (invariant peg)
1 credit          = 10_000_000 µ-credits ≈ \$0.01 USD of cost
display credits   = bigint balance // 10_000_000
```

> **Redenomination (2026-05):** `_MICRO_PER_CREDIT` went 1e6 → 1e7,
> revaluing 1 credit from \$0.001 to \$0.01. Only the credit *display
> denomination* changed — the µ↔USD peg (`$1 = 1e9 µ`) and every stored
> µ-credit balance are unchanged, so displayed credit numbers shrank 10×
> with no change to anyone's USD value. Whole-credit configs (β,
> welcome grant) were divided by 10 in the same change.

Cost is computed in floats during one short helper
(`rates.cost_micro_credits`), then `math.ceil`'d into the integer
µ-credit space. Everything downstream is integer.

### Pre-deduct (reserve) → settle (true-up)

```
turn starts            →  reserve max-estimate    (debit balance)
worker finishes turn   →  read actual usage       (from session_turn)
settle path            →  refund (reserved-actual) + record actual cost
```

Why pre-deduct rather than post-debit:

| Approach | OK for | Not OK for |
|---|---|---|
| **Pre-deduct** (ours) | Variable, expensive turns (agent platform: \$0.10–\$5 per call, multi-minute) | Negligible-per-call APIs |
| **Post-debit** (LiteLLM) | High-QPS chat completion (\$0.001 per call) | Anything where a single call can blow the budget |

OneAPI uses pre-deduct for the same reason — LLM API gateways have
variable costs they want to bound at the start, not regret at the end.

### Worker stays credit-blind

The worker writes `session_turn.input_tokens` / `output_tokens` in
its existing fenced terminal transaction. That's a **metering**
concern, not a credit concern — the column would be useful even if
we didn't have credits (analytics, debugging, capacity planning).

Backend reads those columns to compute cost. Worker has no `import
app.services.credits` anywhere. Two services stay independently
deployable.

### Rate snapshot frozen on reservation

The reservation row carries `rate_snapshot` (a JSONB copy of the
rate-card row at reserve time). Settle uses that snapshot, never the
live `settings.credit_rates`.

Why: if we change prices today, every reservation opened yesterday
still settles at yesterday's rate. Customer-trust property —
"the price I saw when I clicked send is the price I pay."

### Idempotency keys on every external write

`apply_topup` / `apply_adjust` require `idempotency_key` and the DB
has a `UNIQUE` constraint on the column. Retry-on-network-blip is
safe: the second attempt fails the constraint, gets caught, and
`TopupAlreadyApplied` is raised — caller treats as success.

`reserve` / `settle` / `refund` are idempotent via the reservation's
state machine instead: each transitions are guarded by the `state =
'OPEN'` predicate, so a duplicated trigger is a no-op.

### Best-effort welcome grant outside the register tx

Welcome grant on registration is **not** inside the user-creation
transaction. Two reasons:

1. `apply_topup` calls `db.rollback()` itself on a duplicate-key
   `IntegrityError` (documented contract). If composed into the
   register tx, that rollback would wipe the freshly created
   user/org/member rows too.
2. Any transient failure during the grant shouldn't invalidate a
   registration the user already proved with email verification —
   they'd just have to re-verify a new code, which is awful UX.

Trade-off: there's a microsecond window where the user exists with 0
balance. Acceptable; the client doesn't have a session yet because
the registration response hasn't returned.

---

## 3. Data model

Three new tables and one cache column on `user`.

### `credit_ledger` — append-only journal

```sql
CREATE TABLE credit_ledger (
    id              bigserial PRIMARY KEY,
    user_id         integer NOT NULL REFERENCES "user"(id) ON DELETE RESTRICT,
    amount          bigint NOT NULL,                  -- signed µ-credits
    kind            credit_ledger_kind NOT NULL,      -- enum
    reference       jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key text UNIQUE,                      -- required on TOPUP / ADJUST
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_credit_ledger_user_created
    ON credit_ledger (user_id, created_at DESC);
```

**Never `UPDATE`. Never `DELETE`.** Corrections go through new
`ADJUST` rows. The whole row's content is its own history.

#### `kind` enum (`credit_ledger_kind`)

| value | who writes it | sign | meaning |
|---|---|---|---|
| `TOPUP` | admin script / welcome grant | + | money in |
| `RESERVE` | `queue_user_message` at turn start | − | hold pessimistic max cost |
| `SETTLE` | settle path (lazy sweep / sweeper) | both | two rows: reverse-reserve + actual debit |
| `REFUND` | settle path on missing usage / session delete / expired | + | full refund |
| `ADJUST` | admin (no CLI yet; call service directly) | either | manual correction |

`SETTLE` is unusual — every settle writes **two** rows in one tx:

1. `+reservation.reserved` reverse-reserve
2. `-actual_micro` debit at the snapshot rate

This lets the ledger tell the full story per turn instead of just
the net delta.

### `credit_reservation` — lifecycle state machine

```sql
CREATE TABLE credit_reservation (
    id            bigserial PRIMARY KEY,
    user_id       integer NOT NULL REFERENCES "user"(id) ON DELETE RESTRICT,
    session_id    uuid REFERENCES session(id) ON DELETE SET NULL,
    turn_id       bigint,
    reserved      bigint NOT NULL,
    rate_snapshot jsonb NOT NULL,
    state         credit_reservation_state NOT NULL DEFAULT 'OPEN',
    expires_at    timestamptz NOT NULL,              -- sweeper sweep cue
    created_at    timestamptz NOT NULL DEFAULT now(),
    settled_at    timestamptz
);

-- partial index: only OPEN rows; SETTLED/CANCELLED are append-only
-- archive data and don't need to be indexed for the hot path
CREATE INDEX ix_credit_reservation_open_user
    ON credit_reservation (user_id) WHERE state = 'OPEN';

CREATE UNIQUE INDEX uq_credit_reservation_open_session
    ON credit_reservation (session_id)
    WHERE state = 'OPEN' AND session_id IS NOT NULL;
```

#### State machine

```
                ┌───────────┐
                │  OPEN     │ ──[settle_turn]──→ SETTLED  (terminal)
                │ (reserved)│
                │           │ ──[refund]────────→ CANCELLED (terminal)
                └───────────┘
```

`SETTLED` and `CANCELLED` are terminals. Once there, never re-opens.
Corrections live in `credit_ledger` as `ADJUST` rows, not by mutating
old reservations.

### `user.credit_balance` — cache

```sql
ALTER TABLE "user"
    ADD COLUMN credit_balance bigint NOT NULL DEFAULT 0;
```

The cache. Always equal to `SUM(credit_ledger.amount WHERE user_id =
X)` — see §9 invariant check.

### `organization` + `organization_member` — V3 identity

Every user belongs to at least one organization (their personal one,
auto-created at registration). Mirrors OpenAI's model. Currently
**not used for billing** — billing still attaches to `user_id`.
This is documented in `credits-future.md` as a V4 migration point.

See `app/models/organization.py` for the full schema; the design
note is short because the org concept here is identity-only for V3.

---

## 4. Concept reference

| Term | What |
|---|---|
| **µ-credit** | The storage unit. Integer bigint. 1 µ-credit ≈ \$0.000000001 USD of LLM cost. |
| **credit** | The display unit. `credits = µ-credits // 10_000_000`. 1 credit ≈ \$0.01 USD. |
| **reserve** | An OPEN reservation row + a negative ledger entry. Holds funds before the worker starts. |
| **settle** | The pair of ledger rows that close out a reservation with the actual cost. State → SETTLED. |
| **refund** | Closing out a reservation with zero actual cost (worker died, session deleted, usage missing). State → CANCELLED. |
| **rate snapshot** | The frozen copy of the rate-card row stored on the reservation. Settle uses this, not the live `settings.credit_rates`. |
| **rate card** | `settings.credit_rates`: per-model `{in_per_1k_usd, out_per_1k_usd, token_markup, model_fee_credits, reserve_max_output_tokens}` plus global `credit_per_second_credits`. |
| **token markup** | Multiplier on token provider cost only. Covers gross margin + FX volatility for the LLM-cost component. |
| **welcome grant** | One-time TOPUP at registration. `settings.credit_welcome_grant_credits`, default 500 (= \$5 at the post-2026-05 peg). |
| **invariant** | `user.credit_balance == SUM(credit_ledger.amount)`. Always. Cron-checkable. |

---

## 5. Cost formula

The math used at both reserve and settle. Two callers, one helper
(`rates.cost_micro_credits`):

- **reserve** plugs in pessimistic upper bounds for each input so
  the hold covers the worst plausible turn.
- **settle** plugs in actual measurements from `session_turn` so the
  user pays for what they used.

Same formula, different inputs.

### Unit system

```
1 credit          = 10_000_000 µ-credits    ≈ $0.01 USD of LLM cost
1 USD             = 1_000_000_000 µ-credits  (the conversion peg, invariant)
```

All ledger amounts are stored as µ-credit `bigint`. `ceil` happens
exactly once, at the µ-credit boundary, so sub-µ-credit fractions
always round in the operator's favor (billing-safe rounding).

### Three-factor formula

```
cost  =  α · tokens_cost(model)   ← per-model token COGS × markup
       + β · turn_seconds          ← global infrastructure occupancy fee
       + γ · model_fee(model)      ← per-model fixed per-turn premium
```

Expanded into µ-credit arithmetic — this is what
`cost_micro_credits` does literally:

```
token_micro     = ( input_tokens  / 1000 · in_per_1k_usd
                  + output_tokens / 1000 · out_per_1k_usd )
                · token_markup            ← α
                · 1_000_000_000           ← USD → µ-credits

duration_micro  = max(0, turn_seconds)
                · per_second_credits      ← β
                · 10_000_000              ← credits → µ-credits

fee_micro       = model_fee_credits       ← γ
                · 10_000_000

cost_micro      = ceil(token_micro + duration_micro + fee_micro)
```

### Reserve vs settle — same call, different inputs

| input            | reserve                                                | settle                                            |
|------------------|--------------------------------------------------------|---------------------------------------------------|
| `input_tokens`   | `credit_reserve_max_input_tokens` (16_000)             | `session_turn.input_tokens`                       |
| `output_tokens`  | `rate.reserve_max_output_tokens` (per-model: 4096/8192)| `session_turn.output_tokens`                      |
| `turn_seconds`   | `credit_reserve_max_turn_seconds` (1860)               | `session_turn.terminated_at - session_turn.created_at` |
| `rate snapshot`  | live config row at reserve time                        | the snapshot frozen on the reservation            |

At settle the rate snapshot comes from
`credit_reservation.rate_snapshot` (JSONB, frozen at reserve time)
— `cost_micro_credits` never reads live config. A price change
between reserve and settle does **not** re-price the open turn:
the cost shown at start is the cost actually charged.

### Rate card (`config.Settings.credit_rates`)

Per-model:

| model_id          | in_per_1k_usd | out_per_1k_usd | α (token_markup) | γ (model_fee_credits) | reserve_max_output_tokens |
|-------------------|--------------:|---------------:|-----------------:|----------------------:|--------------------------:|
| claude-opus-4-7   | 0.005         | 0.025          | 3.0              | 0                     | 4096                      |
| claude-opus-4-6   | 0.005         | 0.025          | 3.0              | 0                     | 4096                      |
| claude-sonnet-4-6 | 0.003         | 0.015          | 3.0              | 0                     | 8192                      |
| kimi-k2           | 0.00055       | 0.0022         | 3.0              | 0                     | 8192                      |
| HKGAI-V1          | 0.001         | 0.005          | 3.0              | 0                     | 8192                      |
| HKGAI-V3          | 0.001         | 0.005          | 3.0              | 0                     | 8192                      |

Global (not per-model):

| knob                                | default | meaning                                          |
|-------------------------------------|---------|--------------------------------------------------|
| `credit_per_second_credits` (β)     | **0.0** | per-second infrastructure occupancy fee          |
| `credit_reserve_max_turn_seconds`   | 1860    | reserve-time upper bound on duration (31 min)    |
| `credit_reserve_max_input_tokens`   | 16_000  | reserve-time upper bound on input                |
| `credit_welcome_grant_credits`      | 100_000 | auto-grant at registration                       |

`token_markup` (α) is per-model so promotional pricing on a single
model is one number change. β / γ default 0 keeps day-1 behavior
at "tokens only" — see *Day-1 collapse* below.

### Day-1 collapse — currently a tokens-only formula

Because β and γ are both 0 in the default config, the formula
currently reduces to:

```
cost_credits  ≈  ( in_tokens/1000 · in_per_1k_usd
                 + out_tokens/1000 · out_per_1k_usd )
              · token_markup
              · 1000           ← USD → credits ($1 = 1000 credits)
```

Worked example. `claude-opus-4-7` turn with 5 000 input / 2 000
output tokens:

```
= ( 5 · 0.005 + 2 · 0.025 ) · 3.0 · 1000
= ( 0.025 + 0.050 ) · 3 000
= 225 credits
```

Turning on β before the `terminated_at` distribution has stabilized
in production is intentionally avoided — see §8 *Failure modes*
("Duration billing enabled but worker does not write
`terminated_at` yet").

### Reserve sizing — why it can over-hold

Reserve uses the very upper end of every input: `max_input × max_output × max_turn_seconds`. For an average claude-opus-4-7 turn at default config the reserve is:

```
16 000 in × $0.005/k = $0.080
+ 4 096 out × $0.025/k = $0.102
= $0.182 token COGS
× 3.0 markup × 1000 credits/$  → 547 credits reserved
```

Settling that with the 225-credit example above refunds 322
credits. Customers see a transient hold ≈ 2.4× the typical
settled cost; this is intentional headroom — the cost of not
blocking on per-stream cost estimation mid-turn.

### Code map

| concern                    | file:func                                                                       |
|----------------------------|---------------------------------------------------------------------------------|
| formula                    | `app/services/credits/rates.py::cost_micro_credits`                             |
| reserve                    | `app/services/credits/rates.py::reserve_micro_credits`                          |
| snapshot                   | `app/services/credits/rates.py::snapshot_rate`                                  |
| legacy snapshot fallback   | `app/services/credits/rates.py::_normalize_rate_snapshot`                       |
| rate card + β              | `backend/config.py::Settings.credit_rates`, `credit_per_second_credits`         |

---

## 6. Flows

The system has five core operations. All run inside the caller's
transaction unless otherwise noted; the `_internal.insert_ledger_
and_bump_balance` helper takes care of the `SELECT … FOR UPDATE` on
the user row so concurrent ops never race the balance.

### 6.1 Top-up (admin)

Path: `scripts/topup_credits.py` → `credits.apply_topup` →
`_internal.insert_ledger_and_bump_balance`.

```
1. lock user row (FOR UPDATE)
2. INSERT credit_ledger
   - amount = +amount_micro
   - kind = TOPUP
   - reference = {reason, admin_id, source: 'admin'}
   - idempotency_key = <required>
3. UPDATE "user" SET credit_balance = credit_balance + amount_micro
4. caller commits
```

Idempotency: the `UNIQUE` constraint on `idempotency_key` catches a
duplicate. `apply_topup` catches the `IntegrityError`, rolls back
its own work, and raises `TopupAlreadyApplied`. The script treats
that as success and prints the current balance.

### 6.2 Welcome grant (auto, post-registration)

Path: `app/services/auth/register.register_user` → `credits.apply_topup`.

Same as 5.1, with `idempotency_key = f"welcome-{user.id}"` and
`reason = "welcome grant"`. **Runs in a separate transaction** from
the user/org/member creation — see §2 ("Best-effort welcome grant
outside the register tx") for why.

Failure of the welcome grant logs loudly but does **not** fail
registration. The user is created with 0 balance; admin tops up
manually.

### 6.3 Reserve (at turn start)

Path: `queue_user_message` → `credits.reserve_for_turn`.

```
1. lock user row (FOR UPDATE)
2. load rate snapshot for session.model_id
   (raises KeyError if model is unpriced — surfaces as 500; rate
   misconfiguration is an ops bug, not a user error)
3. compute max-cost estimate:
     token_micro    = ((max_in/1000 × in_rate
                       + max_out/1000 × out_rate)
                       × token_markup × 1e9)
     duration_micro = credit_reserve_max_turn_seconds
                      × credit_per_second_credits × 1_000_000
     fee_micro      = model_fee_credits × 1_000_000
     reserved       = ceil(token_micro + duration_micro + fee_micro)
   max_in = settings.credit_reserve_max_input_tokens (default 16000)
   max_out = rate.reserve_max_output_tokens
4. if balance < reserved:
     raise InsufficientCreditsError → HTTP 402
5. INSERT credit_ledger (amount=-reserved, kind=RESERVE,
                         reference={session_id, turn_id, model_id})
6. INSERT credit_reservation (state='OPEN',
                              rate_snapshot=copy of rate row,
                              expires_at=now() + 4h)
7. caller commits
```

`expires_at` (now() + 4h) is the sweeper's "worker probably died"
cutoff. It must exceed the longest legitimate turn, since a live
long turn and a dead worker look identical to the sweeper — see
the *fixed-timeout vs. long turns* hazard in
`docs/credits-future.md` (heartbeat-based passive cancel is the
planned real fix).

### 6.3.1 Transport model — there is no queue XADD

A common misread (and an error in early commit messages): "credits
are refunded if the XADD to the worker queue fails." **There is no
XADD and no Redis Stream queue.** The whole enqueue is one DB
transaction in `queue_user_message`:

```
BEGIN
  lazy-sweep this user's stale OPEN reservations
  INSERT session_turn (status='pending')        ← this row IS the queue item
  reserve_for_turn(...)                          ← may raise 402
  INSERT message (USER) + flip session.status='running'
COMMIT
publish_wake(redis)   ← best-effort pub/sub nudge, AFTER commit
```

- The worker discovers turns by **polling `session_turn`** (5s
  fallback) plus an optional `notify:worker` pub/sub *wake* so an
  idle worker doesn't wait a full poll cycle. The pub/sub carries
  no payload and no queue semantics — losing it only delays pickup
  by one poll interval.
- Credit shortfall is **not** an "XADD failed → refund" path.
  `reserve_for_turn` raising `InsufficientCreditsError` aborts the
  single transaction, so the `RESERVE` ledger row, the
  `session_turn`, and the user message are **never committed** —
  there is nothing to refund. The user sees a 402 and no turn was
  ever enqueued.
- A failed `publish_wake` (after commit) is logged and ignored; the
  poll fallback still picks the row up. It never triggers a refund.

### 6.4 Settle (after agent terminates)

Trigger paths (any of these can fire; all delegate to the same
`settle_open_for_session` / `settle_open_for_user` helpers):

| trigger | source | when it fires |
|---|---|---|
| lazy sweep | `queue_user_message` (next turn) / `GET /credits` | user takes any action after their turn finished |
| subscriber | `app/workers/settle_subscriber.run_forever` | within ~1 s of worker committing a terminal `session_turn` (worker publishes on `settle:notify` post-commit) |
| sweeper | `app/workers/settle_sweeper.run_forever` | every 60 s, regardless of user activity |

There **is no eager-on-SSE settlement** anymore. V1 had one; V2
removed it after we moved usage storage from the Redis stream
(ephemeral) to `session_turn` columns (durable).

```
1. SELECT credit_reservation WHERE state='OPEN'
   AND user_id=X (lazy) OR id=Y (sweeper)
   FOR UPDATE
2. read session_turn.input_tokens / output_tokens and
   turn_seconds = session_turn.terminated_at - session_turn.created_at
3. if either is NULL:
     refund the reservation (zero actual cost, state → CANCELLED)
     stop here
4. compute actual cost using the FROZEN rate_snapshot, not live config:
     ceil(token_cost × token_markup
          + turn_seconds × credit_per_second_credits × 1_000_000
          + model_fee_credits × 1_000_000)
5. INSERT credit_ledger (amount=+reservation.reserved, kind=SETTLE,
                         reference={reservation_id})
                                                     ← reverse reserve
6. INSERT credit_ledger (amount=-actual_micro, kind=SETTLE,
                         reference={reservation_id, turn_id,
                                    actual: {in, out, turn_seconds}})
                                                     ← debit actual
7. UPDATE credit_reservation SET state='SETTLED', settled_at=now()
8. caller commits
```

Idempotent via the `state = 'OPEN'` predicate. A second call sees
the reservation in a terminal state and returns immediately.

### 6.5 Refund (zero actual cost)

Trigger: same paths as settle. Routes when:

- session was deleted (`session_id IS NULL` on the reservation
  after the `SET NULL` FK fires)
- session reached a terminal state but `session_turn` tokens are
  NULL (worker died before the fenced write, or `fail_turn`
  deliberately left them NULL)
- `expires_at` has passed and session never finished (worker
  crash between RESERVE and the fenced terminal write)

```
1. lock reservation FOR UPDATE; bail if not OPEN (idempotency)
2. INSERT credit_ledger (amount=+reservation.reserved, kind=REFUND,
                         reference={reservation_id, cause: ...})
3. UPDATE credit_reservation SET state='CANCELLED', settled_at=now()
4. caller commits
```

### 6.6 Adjust (manual correction)

Service-only path (`credits.apply_adjust`). Same shape as TOPUP but
allows signed amounts and requires a non-empty `reason`. The CLI passes
an operator-typed `--reason`; the admin console stopped asking for one
and passes the fixed `"admin console adjustment"` instead, so on that
path `admin_id` is what identifies the correction. Used for:

- Clawing back from a misconfigured rate card
- Crediting users affected by an incident
- Manually balancing books after a reconciliation finds drift

No CLI yet — invoke the service from a Python REPL with a
deterministic `idempotency_key` (e.g. `incident-2026-05-12`).

---

## 7. Settlement triggers and multi-node coordination

Three things drive settlement, in decreasing latency:

```
┌─────────────────────────────────────────────────────────────┐
│ user takes an action      → lazy sweep in same request      │
│ (milliseconds)              ↓                                │
│                             settle their open reservations   │
│                                                             │
│ worker commits a terminal → settle subscriber via            │
│ session_turn (~1 s)         settle:notify pub/sub            │
│                             ↓                                │
│                             settle that specific session     │
│                                                             │
│ no signal arrives, 60 s   → background sweeper              │
│ (seconds)                   ↓                                │
│                             scan all OPEN reservations,      │
│                             settle/refund the eligible ones  │
│                                                             │
│ sweeper process crashes   → Postgres connection close       │
│ (no recovery needed)        auto-releases the advisory lock  │
│                             next host's sweeper picks it up  │
└─────────────────────────────────────────────────────────────┘
```

### Lazy sweep

Two entry points:

- `app/services/session/enqueue.queue_user_message` —
  `settle_open_for_user(db, user_id)` runs before the new reserve.
  Stops the user from getting a false 402 because their previous
  reservation hadn't been credited back yet.
- `app/api/public/credits.get_credits` — same, so `GET /credits`
  always returns a fresh balance.

Each visits at most a handful of OPEN reservations per call
(partial index keeps the lookup cheap).

### Background process (settle sweeper + subscriber)

One process, two always-on concurrent async tasks (`app/workers/run.py`
with an `asyncio.TaskGroup`; the budget enforcer is a flag-gated third). Standalone entrypoint:
`uv run python -m app.workers.run`. Lifecycle commands:
`bash scripts/deploy.sh background {start|stop|restart|status|logs}`.

#### Sweeper (`app/workers/settle_sweeper.run_forever`)

The polling backstop. Per tick:

1. Try `pg_try_advisory_lock(0x53544c5357454550)` — bytes
   `b"STLSWEEP"` interpreted as bigint, easy to spot in `pg_locks`.
2. If another sweeper holds it: skip this tick.
3. Otherwise: list up to 100 OPEN reservations whose session is
   terminal, deleted, or whose `expires_at` has passed.
4. For each candidate, open its own session (so one row's DB error
   doesn't poison the rest of the batch) and route to the shared
   settle/refund helpers.
5. Release the advisory lock.

Per-row session isolation is load-bearing: SQLAlchemy doesn't
auto-rollback on flush error inside a transaction, so a single bad
row would otherwise poison the whole tick.

#### Subscriber (`app/workers/settle_subscriber.run_forever`)

The push half. Subscribes to Redis `settle:notify`; the worker
publishes the session_id immediately after committing a terminal
`session_turn`. On each message: parse the UUID, open a session,
call `settle_open_for_session`, commit. Per-message try/except keeps
a single bad message from killing the loop; an outer reconnect loop
recovers from pub/sub disconnects with a 1 s backoff.

Pub/sub is fire-and-forget on purpose — a lost message (subscriber
restarting, network blip) is caught by the sweeper on its next 60 s
tick. The subscriber is a latency optimization, not a correctness
path.

### Multi-node coordination

`pg_try_advisory_lock` is **session-scoped, auto-released on
connection close**, and lives in Postgres — so the lock works
across machines without Redis, ZooKeeper, etc.

Compared to Redis-based distributed locks:

- No TTL to renew (which avoids the classic split-brain after a
  TTL expiry)
- Crashed sweeper releases the lock for free (connection close)
- Costs one round-trip per tick instead of N for renewal

This is the [pattern most people end up at](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html)
once they realize they don't need Redlock for a single-DB workload.

---

## 8. Failure modes (and what catches them)

| Scenario | How the system reacts |
|---|---|
| User has 0 balance, sends message | Reserve fails → 402 → user sees "insufficient credits" |
| Reserve passes, worker runs normally, then user closes tab | Lazy sweep on next user action OR sweeper (≤60 s later) settles |
| Reserve passes, worker crashes mid-turn | `session.status` stays RUNNING, `session_turn.input_tokens/output_tokens` stay NULL. Sweeper sees `expires_at < now()` → refund |
| Reserve passes, worker finishes, session deleted before settle | FK SET NULL clears `reservation.session_id`. Sweeper sees `session_id IS NULL` → refund |
| Worker writes 0 in / 0 out (legitimately empty turn) | Both columns are 0 (not NULL) → cost = 0 → settles as +reserved / -0 (full refund of reservation; user pays nothing) |
| Settle interrupted by DB blip | Per-row isolation in sweeper + state-machine idempotency → next tick retries |
| Two concurrent requests from the same user attempt to reserve | `SELECT … FOR UPDATE` on the user row serialises them; one sees the updated balance |
| Welcome grant fails post-registration | Registration succeeds; user has 0 balance; loud log; admin tops up manually |
| Sweeper process killed mid-sweep | Advisory lock auto-released on connection close. Next tick (or next host's sweeper) picks up; settle is idempotent so re-doing the same reservation is safe |
| Token-usage migration applied but old worker not deployed yet | Old worker doesn't write `input_tokens` / `output_tokens` → backend settle reads NULL → refunds the reservation (user gets a free turn until worker upgrades). See `credits-future.md` for the deployment-order convention |
| Duration billing enabled but worker does not write `terminated_at` yet | Backend settles with `turn_seconds = 0.0` → token cost and model fee are charged, but β duration is not. Deploy the worker terminal-write change before turning β above 0 |

---

## 9. Operational runbook

### Check the invariant

```bash
uv run python -c "
import asyncio
from sqlalchemy import text
from app.database import engine
async def main():
    async with engine.connect() as c:
        r = await c.execute(text('''
            SELECT u.id, u.credit_balance,
                   COALESCE((SELECT SUM(amount) FROM credit_ledger WHERE user_id=u.id), 0) AS ledger_sum
            FROM \"user\" u
            WHERE u.credit_balance != COALESCE((SELECT SUM(amount) FROM credit_ledger WHERE user_id=u.id), 0)
        '''))
        broken = list(r)
        if broken:
            print('INVARIANT BROKEN for users:', broken)
        else:
            print('OK — invariant holds for all users')
asyncio.run(main())
"
```

If this ever reports broken users, **stop billing operations and
investigate**. The likely causes:
- A bug allowed a balance update outside `_internal._insert_ledger`.
- A migration interfered with the column.
- Manual SQL touched `credit_balance` directly.

### Top up a user

```bash
uv run python -m scripts.topup_credits \
    --user-id=42 \
    --credits=10000 \
    --idempotency-key=<unique-string> \
    --reason="<audit text>"
```

Re-running with the same `--idempotency-key` is safe — prints
"already applied" and exits 0.

### Inspect a user's ledger

```bash
uv run python -c "
import asyncio
from sqlalchemy import text
from app.database import engine
async def main():
    async with engine.connect() as c:
        r = await c.execute(text('''
            SELECT id, amount, kind, reference, idempotency_key, created_at
            FROM credit_ledger
            WHERE user_id=:u
            ORDER BY id
        '''), {'u': 42})
        for row in r: print(row)
asyncio.run(main())
"
```

### See current `pg_locks` advisory locks (debugging the sweeper)

```sql
SELECT pid, classid, objid, granted, mode
FROM pg_locks
WHERE locktype = 'advisory';
```

`objid = 6004508124021867856` (= `0x53544c5357454550`, bytes
`b"STLSWEEP"`) is the settle-sweeper advisory lock.

### Run the sweeper manually

```bash
# As a one-shot tick from a Python REPL:
uv run python -c "
import asyncio
from app.workers.settle_sweeper import _tick
print(asyncio.run(_tick()), 'reservations processed')
"

# As the long-running process:
uv run python -m app.workers.run
# or via the deploy script:
bash scripts/deploy.sh background start
```

### Find users who never got the welcome grant

```sql
SELECT u.id, u.username, u.credit_balance
FROM "user" u
WHERE u.credit_balance = 0
  AND NOT EXISTS (
      SELECT 1 FROM credit_ledger l
      WHERE l.user_id = u.id AND l.idempotency_key = 'welcome-' || u.id
  );
```

These are users who registered before the welcome grant landed.
Top them up manually with `scripts/topup_credits.py` using a
clearly-named `--idempotency-key=backfill-welcome-{user_id}`.

---

## 10. What's not implemented (deferred)

See [`credits-future.md`](credits-future.md) for the design of each:

- **Worker-side cost cap** — physical circuit breaker that stops a
  runaway turn before it can exceed `balance × 1.2`. Compensates for
  the pre-deduct estimate being an upper bound rather than a hard
  limit.
- **Multi-tier org billing** — moving `credit_balance` from user to
  organization, adding budget hierarchy (org / team / project / user).
- **Payment integration** — Stripe / Alipay / WeChat Pay webhook
  paths that emit `TOPUP` ledger rows with `source` other than
  `admin`.
- **Multiplier system** — group / model / promo multipliers like
  OneAPI uses. Requires real operational pressure to justify.
- **Auto reconciliation cron** — daily SQL invariant check with an
  alert. Currently a manual `uv run python -c …` (see §9).

---

## 11. Version history

| Version | What landed |
|---|---|
| **V1** | Initial design: `credit_ledger`, `credit_reservation`, `user.credit_balance`. Reserve / settle / refund / topup / adjust primitives. Lazy sweep on user actions. Settle source = Redis stream tail. SSE eager-settle hook. |
| **V1.5 cleanup** | `align unique indexes as constraints` migration (cosmetic — DB layout aligned with model declarations) |
| **V2** | Durable usage on `session_turn.input_tokens/output_tokens/terminated_at`, written by the worker in its fenced terminal transaction. Backend settle reads from there, no longer from the ephemeral Redis stream. SSE eager-settle hook removed (Redis was the only reason it existed). |
| **V2 → sibling-process** | Sweeper extracted from the FastAPI lifespan into a standalone process (`app/workers/run.py`), deployed via `deploy.sh background`. Web returns to pure HTTP responsibility. Multi-node coordinated via Postgres advisory lock. |
| **V2 billing cleanup** | Open reservation uniqueness is enforced with `uq_credit_reservation_open_session`; unused `credit_reservation.kind` / `sandbox_id` were removed after turn-level three-factor billing made the reserved sandbox variant obsolete. |
| **V3** | Organization concept (mirrors OpenAI). Personal org auto-created at registration. Welcome grant config (default 100_000 credits) — runs as a separate, best-effort transaction after the user/org/member commit. Note: billing still attaches to `user_id`; org-level billing is a V4 migration documented in `credits-future.md`. |
| **V4 (planned)** | One of: worker-side cost cap, multi-tier org billing, payment integration. Driven by business need. |

---

## 12. Where to look in the code

| Concept | File |
|---|---|
| Public credit API | `app/services/credits/__init__.py` (re-exports) |
| Atomic ledger + balance write | `app/services/credits/_internal.py` (the only file in the codebase that writes to `credit_ledger`) |
| Reserve | `app/services/credits/reserve.py` |
| Settle / refund | `app/services/credits/settle.py` |
| Top-up | `app/services/credits/topup.py` |
| Adjust | `app/services/credits/adjust.py` |
| Read balance / history | `app/services/credits/balance.py`, `history.py` |
| Rate card + cost math | `app/services/credits/rates.py` |
| Sweeper (poll loop) | `app/workers/settle_sweeper.py` |
| Subscriber (push, `settle:notify`) | `app/workers/settle_subscriber.py` |
| Background process entry | `app/workers/run.py` |
| Lazy-sweep call site (enqueue) | `app/services/session/enqueue.py::queue_user_message` |
| Lazy-sweep call site (read) | `app/api/public/credits.py` |
| Welcome grant call site | `app/services/auth/register.py::register_user` |
| Organization model | `app/models/organization.py` |
| Rate card config | `config.py::Settings.credit_*` keys |
| Migrations | `alembic/versions/9e3a8b714f02_*` (V1 schema), `b1c7e29f4d8a_*` (cosmetic drift fix), `c2f4a8d1e9b3_*` (V2 token columns), `d8f3b2e6a1c9_*` (V3 organization) |
| Admin top-up script | `scripts/topup_credits.py` |
| User seed script (creates personal orgs too) | `scripts/seed_users.py` |
| E2E test fixture | `tests/conftest.py::test_user` |
