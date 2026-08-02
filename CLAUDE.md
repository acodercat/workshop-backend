# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Scope

Backend module of the metaseq-computer monorepo. See `../CLAUDE.md` for monorepo-level context (axon, agent-worker, frontend). This file covers backend-only concerns.

## Commands

```bash
# Install / sync deps
uv sync

# Dev server (API on :8005, root_path="/api/v1")
uv run uvicorn app.main:app --host 0.0.0.0 --port 8005 --reload

# Background process (separate from the web server; runs the credit-settle
# sweeper + subscriber, the payment reconciler, and — when
# ``budget_enforcer_enabled`` — the mid-turn budget enforcer). Run alongside
# the API in its own terminal:
uv run python -m app.workers.run

# Production — single-host manual deploy via scripts/deploy.sh. Top-level
# commands act on BOTH siblings (web + background):
bash scripts/deploy.sh deploy       # stop both + uv sync + alembic upgrade + start both
bash scripts/deploy.sh restart      # code-only restart of both (no deps / no migration)
bash scripts/deploy.sh status       # report state of BOTH web (:8005) and background
bash scripts/deploy.sh logs         # tail -f logs/uvicorn.log
bash scripts/deploy.sh background start|stop|restart|status|logs   # the background process alone
# Web truth source is ``lsof -t -i:8005``; background's is ``pgrep -f 'app.workers.run'``
# (no port). Neither uses a PID file, so both tolerate instances started
# outside the script. uvicorn stdout → logs/uvicorn.log, background stdout →
# logs/background.log; loguru/stdlib app logs go elsewhere under logs/ per
# app/logging_config.py. Multi-node: run both processes on every host —
# the settle sweeper self-elects one active poller via a Postgres advisory lock.

# HTTPS / custom port — script's defaults won't fit; use uvicorn directly:
uv run uvicorn app.main:app --host 0.0.0.0 --port 9090 --workers 1 \
  --ssl-certfile ./cert/<domain>.pem --ssl-keyfile ./cert/<domain>.key

# Migrations
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "description"

# Tests (pytest; asyncio_mode is project-level "auto" — no @pytest.mark.asyncio needed)
uv run pytest tests/                       # ~80 offline unit tests (tests/*.py) — no DB/Redis/network
uv run pytest tests/test_credit_settle_usage.py::test_name
# tests/*.py (unit) run with no external deps; tests/e2e/ is opt-in (see below)

# Full-pipeline E2E (opt-in; needs backend + worker + Redis + Postgres + sandbox running)
RUN_E2E_TESTS=1 uv run pytest tests/e2e/ -v
```

`alembic/env.py` imports `app.models.*` and `config.settings` — migrations rely on `db_uri` from `.env` at repo root.

**The migration history is not squashed, and that is a decision.** Ninety-plus files is slow to replay on a fresh database and holds plenty of steps a later one undid, but collapsing the early ones into a single initial revision is only correct if **every** live database is already at or past the collapse point. One that is not — an unattended staging box, a colleague's local, a restored backup — meets a `down_revision` it has never heard of and stops, or worse, runs `CREATE TABLE` against tables that exist. Squashing is therefore gated on reading `SELECT version_num FROM alembic_version` on every live database and choosing a point at or before the oldest, and it is worth doing only when there are no pending migrations to complicate the answer. The gain is developer-setup time; nothing about it reaches production behaviour.

## E2E tests (`tests/e2e/`)

Opt-in via `RUN_E2E_TESTS=1` (mirrors axon's `RUN_INTEGRATION_TESTS`).
Each test drives the real production pipeline through the public HTTP
API: create session → POST message → consume the per-session SSE
stream until `agent.stopped` → verify the persisted state via
`GET /sessions/{id}` → `DELETE /sessions/{id}`.

Preconditions before running:

- Backend dev server up on `:8005` (or override via `BACKEND_BASE_URL`).
- Agent worker running (`cd ../agent-worker && uv run python -m agent_worker`).
- Postgres + Redis + OpenSandbox reachable per the worker's `.env`.
- Worker's `models.toml` has the model id the test's tier resolves to —
  the e2e test creates a session with the default `STANDARD` tier, which
  `settings.session_tier_models` maps to a concrete model id; make sure
  that id exists in `models.toml`, or adjust the tier map.

The `test_user` fixture in `tests/conftest.py` provisions a unique
throwaway user via raw psycopg (no public registration endpoint exists
by design) and tears down everything that user owns regardless of
pass/fail: cascades through `user → session → message + session_turn`,
plus an explicit `sandbox` sweep keyed on session_id (the sandbox
table is application-managed without a session FK on purpose). One
full happy-path run takes ~15-30s wall time and a few cents of LLM
tokens; sandbox containers spun up during the test get cleaned up by
the OpenSandbox reaper, not by this fixture.

## Architecture

### Request topology

The backend and worker **never talk directly**. Postgres `session_turn` is the queue **and** the ownership truth source; Redis carries only the wake signal and the SSE output stream:

- Backend INSERTs a `session_turn` row (status `PENDING`) and publishes on `notify:worker` (Pub/Sub) to wake the worker. The Postgres row — not Redis — is the queue.
- Worker claims the row (`FOR UPDATE SKIP LOCKED` under a DB-lease `worker_id`), runs the agent, and XADDs events to `session_stream:{session_id}` (Stream). On terminal it writes `session.status` + the assistant `message` directly into the backend's tables in one fenced transaction.
- Backend's `GET /sessions/{id}/stream` XRANGEs (replay) then XREADs (follow) that stream and forwards each entry as SSE.

Redis key/channel names in `app/services/redis.py` (`SESSION_STREAM_PREFIX`, `WORKER_NOTIFY_CHANNEL`, `USER_EVENTS_CHANNEL`, `CANCEL_CHANNEL_PREFIX`, `SETTLE_NOTIFY_CHANNEL`) are a **wire contract** mirrored in `agent-worker/agent_worker/wire/keys.py`. Changing one without the other silently desyncs the services.

### State split: DB vs worker

- **DB is rendering state.** `message.blocks` (JSONB) holds the structured block list the frontend renders. USER rows are inserted in `queue_user_message`; ASSISTANT rows are projected from worker events (the worker writes them — backend does not synthesize assistant rows).
- **Worker owns LLM state.** Conversation history / compacted context is the worker's concern — the backend enqueues only `{session_id, query, files}` and deliberately does **not** send history. The serialized axon `Message` list now persists in the backend-migrated `session_context` table (see below), but only the worker reads/writes it; the backend just owns its schema.
- `session.status` (`app/models/session.py`) has five values across three layers: `IDLE` (initial, stamped by `create_session` on a fresh row — the worker never writes it), `RUNNING` (backend flips here the moment a turn is enqueued), and one of three terminals — `COMPLETE` / `CANCELLED` / `FAILED` — written by the worker atomically with the assistant insert (or with no row, on the error path). The frontend uses this to decide whether to open the SSE stream; gates elsewhere check `== RUNNING` / `!= RUNNING`, not a terminal set.
- A new session carries four creation-time, immutable columns. The client picks a **capability tier** (`tier ∈ {FAST, STANDARD, ADVANCED}`, default `STANDARD`) — never a model name; `create_session` resolves it to `session.model_id` via `settings.session_tier_models` (validated against `settings.supported_session_model_ids`). It also stamps `agent_type ∈ {FULL, LITE}` (sandbox agent vs Q&A+web-search, default `FULL`) and `thinking` (extended-reasoning toggle, default on; shown to users as "deep think"). `tier` / `agent_type` round-trip to the UI; `thinking` and `model_id` do not. The worker selects its actual LLM from its own config keyed by `model_id`, **and** the backend reads `model_id` for the credit rate card at reserve time (`app/services/credits/rates.py`) — a `model_id` absent from `settings.credit_rates` makes the reserve fail, so the column is load-bearing, not an audit field.
- **Soft-delete**: `DELETE /sessions/{id}` sets `session.deleted_at` rather than removing rows (returns 409 while `status == running`). Every read folds `deleted_at IS NULL` into its `session_id`-scoped filter, so a soft-deleted session reads as gone (and its id isn't leaked) while child rows + the credit_reservation back-link are retained. The delete handler settles the session's open reservation first.

### `session_context` table (`app/models/session_context.py`)

The worker's serialized agent memory, **worker-touched / backend-migrated** (same ownership split as `session_turn` and `sandbox`: backend owns alembic, worker owns the rows — written via raw SQL in `agent_worker.infra.session_context_repository`, never read or written by the backend). 1:1 with `session` (PK = `session_id`, `ON DELETE CASCADE`). Holds the axon `Message` dicts (`messages`) — compaction state, provider-opaque thinking signatures, `preserve_in_compact` flags — that the projected `message` table (UI blocks) can't round-trip, plus `model_id` (provider pin / audit, mirrors `session.model_id`) and `todos` (the durable todo checklist). `todos` is a **separate column on purpose**: `todo_write` calls are compactable out of `messages`, so the list gets its own non-compactable home; the worker rewrites it each turn from `Agent.todos` and reseeds via `Agent.run(todos=...)`. This table replaced the worker's former on-disk `{sessions_dir}/{session_id}.json`, so a session resumes on any worker replica.

### Sandbox table (`app/models/sandbox.py`)

One row per OpenSandbox container, **worker-managed** — the backend only declares the schema (so worker-touched columns ride in backend migrations) and sweeps the table on test teardown. **No DB-level FK to `session`** (avoids CASCADE footguns and constraint cost on the worker's hot insert path); reads and cleanup key on `session_id` directly. Multiple rows accrue per session: the worker never kills a container (it only `close()`s the HTTP client, leaving it warm for the next turn) — a **separate reaper process** kills containers idle past a `last_used_at` threshold or whose session is soft-deleted, then flips the row terminal. The active sandbox for a session is its most-recent non-terminal row; terminal rows are never deleted. The status enum and the reaper's transient claim state are documented in the model docstring.

### SSE streaming (`app/services/session/streaming.py`)

Two-phase generator: replay all existing stream entries, then block-read until an `agent.stopped` terminal event arrives. An older version merged `message_chunk` events into one blob on replay and lost tool calls — do not reintroduce that. Cold history is served by `GET /sessions/{id}` (structured `blocks`), not by the SSE endpoint; the stream endpoint is only meaningful while `status == "running"`.

### Per-user notifications `/me/events` (`app/services/user/hub.py`)

Separate from `session_stream`: a lightweight SSE feed carrying idempotent `session.status_changed` for cross-tab sidebar sync. **One Redis `SUBSCRIBE` per process** (the `UserEventHub`, started in the lifespan) fans `user_events:bus` out in-memory to each connection's bounded `asyncio.Queue` — opening `/me/events` allocates a queue, **never a Redis connection**. At-most-once (Pub/Sub) is fine because the payload is idempotent state sync; no replay, no sticky sessions, multi-node native. `USER_EVENTS_CHANNEL` is a wire contract mirrored in the worker (which `PUBLISH`es it) — deploy together. Full design: [`docs/user-events-fanout.md`](docs/user-events-fanout.md).

### Redis client setup (`app/main.py` lifespan)

Two pools, intentionally separate (see `_make_redis_client`):

- **Short-op pool** (`app.state.redis`) — `BlockingConnectionPool` sized by `redis_pool_size` (default 30). XADD, PUBLISH, DELETE, PING. When full, callers wait up to `redis_pool_timeout_seconds` for a connection before raising — trades a tail-latency spike for fewer 5xx during bursts.
- **Streaming pool** (`app.state.redis_streaming`) — plain `ConnectionPool` sized by `redis_streaming_pool_size` (default 100). Holds one connection per active SSE for the full XREAD `block_ms`. Fail-fast on full, because hitting the cap means the box is at its SSE capacity — back-pressure is the right answer.

Sharing a single pool would let SSE long-polls starve short-op traffic. Both pools probe checked-out connections on the 30s `health_check_interval` so a silently-dropped socket surfaces promptly.

Startup uses `_ping_db` / `_ping_redis` with exponential backoff (`startup_probe_*` settings) so a few-second dependency race at boot doesn't crash-loop the pod. Liveness (`/health/live`) is always 200; readiness (`/health/ready`) returns 503 when DB or Redis are unreachable so K8s stops routing to the pod without restarting it.

### Observability (`app/observability.py`)

`install_observability(app)` (called once in `app/main.py`) wires three additive, backend-optional concerns — all no-ops until their backend is configured:

- **Request-id correlation** — `RequestIDMiddleware` reuses a sane inbound `X-Request-ID` (validated against `_REQUEST_ID_RE`, else minted) and binds it into the loguru context so every log line carries `request_id`, echoing it back in the response header. Implemented as **raw ASGI, not `BaseHTTPMiddleware`**, because the latter buffers the response body and would break SSE streaming — this wrapper only touches the response-start message.
- **Sentry** — no-op until `sentry_dsn` is set (`sentry_environment`, `sentry_traces_sample_rate` knobs).
- **Prometheus** — `/metrics` endpoint (gated by `metrics_enabled`, default on) via `prometheus-fastapi-instrumentator`, with `/metrics` + both health probes excluded from its own histograms.

### Enqueue ordering (`app/services/session/enqueue.py`)

`queue_user_message` has a load-bearing sequence:
1. `SELECT ... FOR UPDATE` the session row (`deleted_at IS NULL` in the predicate). Reject 409 (`GenerationInProgressError`) if `status == RUNNING`; raise `SessionGoneError` → 404 if the row is gone (a `delete_session` committed in the window). The lock serializes concurrent enqueues **and** `delete_session` (which takes the same lock before tombstoning). If the send carries `agent_type` (the one-way LITE→FULL mode upgrade — the composer's mode pick is local until the user sends), apply it here under the lock so the flip commits atomically with the turn; FULL→LITE rejects 400 (`AgentTypeDowngradeError`). This is the only `agent_type` mutation path.
2. Lazy-sweep this user's open credit reservations into terminal state.
3. INSERT the `session_turn` (status `PENDING`) and flush for its auto-generated `turn_id`.
4. Reserve credits — raises `InsufficientCreditsError` → 402 if the estimate would push the balance negative; the whole tx rolls back (no message, no turn).
4a. **Concurrent-task cap**: reject 429 (`ConcurrentTaskLimitError`) if the user already has `settings.max_concurrent_running_tasks` (default 10) sessions in `RUNNING`. Enforced right after reserve so it runs under the reservation's user-row `FOR UPDATE` lock — concurrent enqueues for the same user serialize on it, so the count can't overshoot; a reject rolls the whole tx back.
5. INSERT the user `message`, bind uploaded files, flip `status = RUNNING`, then DELETE the previous turn's `session_stream:{id}` — all before one commit. The stream DELETE is **last, after every fallible step**, so a 402/skills failure can't wipe replay history for a turn that never enqueues; before the commit so the stream is clean before the worker (woken post-commit) writes the new turn's first event.
6. `publish_wake` on `notify:worker` **after** commit (so the worker's claim sees the row). Best-effort: a missed publish just costs one worker poll cycle and never raises into the handler.

### Events wire format (`app/common/event_types.py`, `app/common/sse.py`)

`EventType` enum is the backend's independent copy of the strings the worker emits (`agent_worker/wire/session_events.py` — the `SessionEventEmitter` — is the authoritative producer). The backend has no runtime dependency on axon; it only consumes the stream. Redis Streams store `str → str` only, so structured fields (`args`, `files`, `usage`) arrive JSON-encoded and are forwarded **verbatim** to the frontend. `null`/`None` lands on the wire as the literal string `"null"`.

### Auth

JWT (`app/auth.py`), bcrypt passwords hashed/verified off the event loop (`asyncio.to_thread`). Identity is `username` (the auto-generated `axon_${id}`) + `email` + `phone` (both nullable, both `UNIQUE`; a user has whichever channel(s) they registered/verified through); sign-in is username+password (`POST /auth/access-tokens`) or the email-**or-phone** OTP flow below. Revocation is by bumping `user.token_version` — tokens carry the version they were issued against; `_decode_and_get_user` rejects tokens whose version is stale. `revoke_user_tokens` runs after a password, email, **or** phone change (phone is a login + recovery vector, so rotating it invalidates stale tokens the same way). SSE endpoints authenticate with the **header** Bearer token via `authenticate_for_streaming`, which opens a short-lived DB session for the JWT lookup and closes it before the stream begins (so the SSE lifetime doesn't pin a DB connection) — the frontend uses `@microsoft/fetch-event-source`, which can set headers. **Share-view tokens** are a separate JWT class carrying `scope=share-view`: the user-token decoder rejects anything *with* a `scope`, the share decoder rejects anything *without* one, so the two classes can't cross over (see Sharing).

### Registration / verification — email + phone (`app/services/auth/`, `app/services/email/`, `app/services/sms/`)

The 6-digit-code flow is **channel-agnostic** (`app/services/auth/verification.py`): every request carries `channel ∈ {email, phone}` plus an `identifier` (the email address or the China-mainland mobile number). Only the **send step** differs — `email` renders an HTML template and dispatches over **SMTP** (Lark Suite Mail — `app/services/email/client.py` via `aiosmtplib`); `phone` texts via **Aliyun SMS** (短信服务 / dysmsapi, `app/services/sms/`, one seam `send_verification_sms`). The Redis code lifecycle (TTL, resend cooldown, wrong-attempt cap) is identical across both. Phone numbers are normalized to canonical storage form by `app/common/phone.py` (`NormalizedPhone`, strips spaces/dashes, validates `1[3-9]` + 11 digits) before use as a login key.

**Login and registration are separate flows** over that shared dispatch (`POST /auth/verification-codes`, body `{channel, identifier}`). **Login** (`POST /auth/access-tokens-by-code`) is login-only: peek-verify the code, then an existing `ACTIVE` user → consume + issue a JWT (`kind="logged_in"`); an unregistered identifier → `not_registered` (404), steering the client to register. **Registration** (`POST /users`) is **invite-gated**: the body carries an `invite_code`, and a valid invite *is* the access grant, so the user is created `status=ACTIVE` (auto-generated `axon_${id}` username + random password placeholder) and gets a JWT immediately (`kind="logged_in"`, same `LoggedInResponse` as login) — there is no admin-approval/`PENDING` gate. Registration also mints the user's own personal referral code and records the redemption. Invite codes (`app/services/auth/invite.py`, `invite_code` + `invite_redemption` tables) come in two origins: `SYSTEM` (operator-issued via `scripts/create_invite_code.py`, seeds the cold start) and `REFERRAL` (per-user, capped at `invite_referral_max_uses`); the slot is fence-claimed (`claim_invite`, an atomic `UPDATE … WHERE used_count < max_uses … RETURNING`) **before** the OTP burn so an exhausted code never strands a verified code. A successful referral credits the referrer `invite_referral_reward_credits` (default 500, a `REFERRAL` ledger row) best-effort in its own transaction, alongside the welcome credit grant. State for in-flight codes lives in Redis (TTL = `register_code_ttl_seconds`, default 600). Per-email resend cooldown (`register_code_resend_cooldown_seconds`, default 60) and per-code wrong-attempt cap (`register_code_max_attempts`, default 5) are layered with per-IP rate limits on the endpoints themselves. SMTP/SMS creds default to empty so dev boxes without provider access still boot — calls that need them fail loudly at send time, not at startup. **`email_change.py` and its SMS sibling `phone_change.py`** reuse the same code-verification primitive (namespaced `email_change:{addr}` / `phone_change:{num}`) for changing a signed-in user's email or phone: a two-step *sudo-gated* flow (`POST /me/email/verification-codes` → `PUT /me/email`, and the `/me/phone/...` mirror in `app/api/endpoints/user.py`) that verifies `current_password`, pre-checks uniqueness on the new value, sends a code to it, then atomically swaps the column and bumps `token_version`.

### Rate limiting (`app/rate_limit.py`)

slowapi, **keyed on the authenticated user when present, else the client IP** (`_rate_limit_key`: `authenticate_and_get_user` stamps `request.state.user_id`, which FastAPI resolves before slowapi reads the key — so authenticated endpoints are immune to NAT/CGNAT-shared IPs, while pre-login endpoints still key on IP). **Counters stored in Redis** (`storage_uri=settings.redis_url`) so the limit is a true cluster-wide ceiling rather than per-uvicorn-worker (an in-memory store would make `5/minute` effectively `5 × workers` and reset on restart). Degrades gracefully if Redis is down — `in_memory_fallback_enabled` keeps counting locally and `swallow_errors` ensures a storage blip never 500s a request. Limits are config knobs (`rate_limit_*` in `config.py`): `login` 5/min, `message_create` 30/min, `share_unlock` 5/5min, `register_code` `3/minute;20/hour` (layered burst + hourly per OWASP OTP guidance — a `;`-separated multi-limit string slowapi enforces in full), `register` 20/hour, default 200/min. `RateLimitExceeded` is registered as an exception handler in `app/main.py`. If abuse appears, raise the limits behind authenticated identity rather than the per-IP key (NAT'd corp networks otherwise hit the cap collectively).

### Sharing (`app/api/endpoints/share.py`, `session_share` table)

A session owner mints a share record with a password. Recipients hit a public unlock endpoint that swaps the password for a short-TTL **share-view token** (`share_view_token_expire_minutes`, default 15) — a JWT bound to that share, distinct from a normal user access token. The session's blocks are served read-only against that token. Short TTL because the recipient is actively viewing; re-prompting after idle limits the blast radius of a leaked URL.

### Feedback (`app/api/endpoints/feedback.py`, `session.py`)

Two tables, two shapes. `user_feedback` is freeform product feedback (`POST /feedback` in `feedback.py`). `message_feedback` is per-assistant-message thumbs/comment with a unique `(user_id, message_id)` constraint enforced via `IntegrityError → 409` — its endpoints live in `session.py` (as `/sessions/{id}/messages/...` sub-resources), not `feedback.py`.

### File storage (`app/services/file_storage.py`)

Frontend uploads/downloads directly to/from Alibaba OSS via presigned URLs. The backend **never proxies file bytes** — `FileStorage` only mints URLs. Upload URLs validate the extension against the policy's `allowed_types` whitelist (`SESSION_FILE_POLICY.allowed_types`); download URLs embed a ``response-content-disposition`` override so the cross-origin OSS response forces a save-as with the real filename. **Authorization differs by direction.** Minting an *upload* URL is JWT-only (+ the per-user file quota) — the object isn't bound to a session yet. *Download* authz is two-step: `_get_or_404` (caller owns the session) then `assert_file_belongs_to_session` (a `session_files` lookup) — so `session_files` **is** the per-key ownership index, and a probe can't mint a URL for another user's `file_key` against a session it owns (403 on the second check). The UUID-unguessable key + short presigned-URL TTL are defense-in-depth on top; the public share download surface (`/shares/{token}/files/download-url`) applies the same `session_files` binding check against the share's session.

The `session_files` table (`app/models/session_files.py`) is the first-class binding between an OSS `file_key` and a session — the durable index behind the user's cross-session **file library**, served at `GET /sessions/files` (filtered + paginated **server-side** by `origin` / `kind` / search, so totals can't drift from the page). Each row carries an `origin` tag (`UPLOAD` user-provided, `DELIVERABLE` agent-produced, `INTERNAL`) and **denormalized** `filename` / `mime_type` / `size_bytes` so the library lists without round-tripping OSS. "kind" (document / image / video / audio / …) is **not a stored column** — it's a SQL CASE expression (`file_kind_case` over `mime_type` + `filename`, valid buckets in `FILE_KINDS`, both in `app/common/file_kind.py`) built into the query so the same classification drives the filter and the response.

**Orphan-upload reaping** (`oss_orphan_tagging_enabled`, default **off**): a presigned upload that's never bound to a session (user abandons the attachment) leaves a paid OSS object with no `session_files` row. When enabled, `generate_upload_url` signs an `x-oss-tagging: lifecycle=orphan` header into the PUT and returns its value as `tagging_header`; the frontend echoes that exact header (it's signed, so omitting it would 403). On bind, `queue_user_message` clears the tag (`file_storage.clear_orphan_tag`, best-effort + post-commit) so the object survives. A bucket **lifecycle rule** (install via `scripts/setup_oss_lifecycle.py`) deletes objects still tagged `orphan` after ~1 day, so OSS — not a backend cron — does the reaping. The flag stays off until rollout is coordinated: the frontend sends the header **only when the response carries `tagging_header`**, so deploy the frontend first (harmless no-op while off), then add the bucket **CORS `allowed-header` for `x-oss-tagging`** + run the lifecycle script, then flip the flag. Skill bundles don't need this — their OSS upload is bound in the same request, compensated on failure.

### Credits / billing (`app/services/credits/`, `app/workers/`)

PAYG credit ledger in front of the LLM turn pipeline. **Every write to `credit_ledger` / `user.credit_balance` funnels through `credits/_internal.py`** — nothing else touches them directly — which preserves the double-entry invariant `user.credit_balance == SUM(credit_ledger.amount)`. Storage unit is **µ-credits** (1 credit = 10_000_000 µ-credits ≈ $0.01 USD). The µ↔USD peg is invariant at `$1 = 1e9 µ-credits`; the 2026-05 redenomination changed only the credit display denomination (`_MICRO_PER_CREDIT` 1e6→1e7), so stored balances kept their USD value while displayed credit numbers shrank 10×.

Per-turn lifecycle:
1. **Reserve** (`reserve_for_turn`, called inside `queue_user_message`): locks the user row, snapshots the model's rate card onto a `credit_reservation`, debits an estimate sized from the model's `reserve_est_input_tokens` (data-driven per-model p50 — an entry gate + per-user concurrency throttle, **not** a worst-case bound), raises `InsufficientCreditsError` if the balance can't cover it. The rate is **frozen on the reservation** so a config change between reserve and settle never re-prices an open turn (customer-trust property: charged cost == cost shown at start).
2. **Mid-turn enforcement** (`app/workers/budget_enforcer.py`, flag-gated by `budget_enforcer_enabled`): the worker's lease heartbeat reports cumulative usage onto `session_turn.interim_input_tokens`/`interim_output_tokens` (pure token counts — the worker never sees money); the enforcer prices them with the frozen snapshot, projects each user's post-settle balance across their concurrent turns, and stops the worst-overrunning turn via the existing cancel channel when the projection hits the buffer. The turn ends as a normal CANCELLED (billed actuals). Fail-open: enforcer down ⇒ pre-enforcer semantics. Interim columns are telemetry only — settle never reads them. **Why a terminal happened** is `session_turn.termination_reason` (`TerminationReason` StrEnum, plain VARCHAR — descriptive, never drives billing/recovery): the *why* to `status`'s *what*. Every terminal write stamps one (`completed` / `user_cancelled` / `insufficient_credits` / `worker_shutdown` / `model_error` / `timeout` / `empty_output` / `internal_error` / `worker_lost`); the cancel-channel payload carries it (empty ⇒ `user_cancelled`, deploy-order safe) and the `agent.stopped` wire event echoes it so the frontend can say "stopped: out of credits" vs plain cancel. Values mirrored in `agent_worker.infra.database.TerminationReason` — change both repos together.
3. **Settle** (`settle_turn`): posts the actual cost from `session_turn.input_tokens`/`output_tokens` — written by the worker in the same fenced transaction as its terminal status flip, so settle reads durable billing-grade storage, **not** the TTL'd Redis stream (a V1 race that's been removed). Idempotent. `refund_reservation` zeros the hold when usage was never measured (worker crash / lost lease).
4. Cost is three additive factors carried in the snapshot: `α·token_cost + β·turn_seconds + γ·model_fee` (`token_markup`, `credit_per_second_credits`, `model_fee_credits`).

**Two-sided pricing (buy vs sell).** The cost peg above (1 credit ≈ $0.01 of LLM COGS) is the **cost basis**, used to price a turn. Separately, `settings.credit_price_cny` (default ¥0.1/credit) is the **customer-facing RMB sale price** for converting a top-up payment into credits — the domestic-market recharge knob. They are deliberately different: selling at ¥0.1 against a ¥0.072 (≈$0.01) cost basis bakes ~28% margin into the recharge step on top of the per-turn token markup. Only `rates.credits_from_cny` (used by `scripts/topup_credits.py --cny` and any future self-serve payment) touches CNY; **billing/settle/balance never see RMB** — they stay entirely on the µ-credit ledger and the USD cost peg. The internal anchor was intentionally **not** redenominated to RMB: at 93 real users / ¥50万 in real top-ups, a peg change would require migrating live balances (high risk), whereas sell-side pricing achieves "1 credit = ¥0.1" with zero balance migration.

Two trigger paths for settle, push + pull halves of one responsibility:
- **Lazy sweep** (`settle_open_for_user`): user-facing endpoints (`GET /credits/*`, `queue_user_message`) settle that user's open reservations on every hit — covers any user who comes back.
- **Background process** (`python -m app.workers.run`, deployed as the `background` sibling): the `settle_sweeper` polls every 60s (correctness backstop for users who never return — API clients, abandoned sessions, workers that crashed between reserve and the terminal write), and `settle_subscriber` listens on the `settle:notify` pub/sub channel (latency optimization — worker publishes on terminal write, settle fires sub-second). Pub/sub loss is expected and harmless; the sweeper backstops it. The sweeper uses a Postgres advisory lock so exactly one host polls per tick in a multi-node deploy; connection loss auto-releases it.

`apply_topup` and `apply_adjust` (signed correction) are the only balance-increase paths; both are idempotent on a caller-supplied key. `apply_topup` is driven by the admin `topup_credits` script **and** by the payment webhook (see Payments below). See `docs/credits-future.md` for deferred work.

### Payments / recharge (`app/services/payment/`, `app/api/endpoints/payment.py`, `payment_order` table)

Buy-side recharge sitting in front of the credit ledger: a user pays RMB via **汇付斗拱 (Huifu Dougong)** and the ledger is topped up. The ledger layer is **unchanged** — payments only add the order/charge/notify flow and call `credits.apply_topup` exactly once per paid order. Money still funnels through `apply_topup`; the payment layer never writes `credit_ledger` / `user.credit_balance` directly, and `payment_order.ledger_id` back-links the TOPUP row.

- **Provider abstraction**: endpoints/services depend only on the `PaymentGateway` protocol + frozen `ChargeResult` / `VerifiedEvent` dataclasses (`services/payment/provider.py`); the 汇付 impl is `HuifuGateway`. **All 汇付 / `dg-sdk` specifics are confined to `services/payment/huifu.py`** — a second provider is a new adapter + a `PaymentProvider` enum member, nothing else. The SDK is synchronous (`requests`), so `huifu.py` is the one place that bridges sync→async via `asyncio.to_thread`.
- **`payment_order` table** (`app/models/payment_order.py`): worker/backend both backend-owned. UUID PK (opaque in the polled `GET`), `out_trade_no` (UNIQUE, = 汇付 `req_seq_id`) is the provider-facing key. Two **frozen-at-create** amounts: `amount_fen` (RMB cents, integer money) and `credit_micro` (the µ-credit grant via `credits_from_cny` — so a `credit_price_cny` change between scan and pay can't re-price an open order, same trust property as a reservation's `rate_snapshot`). Status `PENDING → PAID / EXPIRED / FAILED`; partial index on `status='PENDING'` backs the reconciler.
- **Three endpoints** (router under `/api/v1/payments/*`): `POST /payments/orders` (JWT + `@limiter.limit`, gated by `payment_enabled`, freezes credits + places the charge + returns the QR), `POST /payments/webhooks/huifu` (**no JWT** — authenticated by RSA signature over the **raw `resp_data` string**, `@limiter.exempt`, returns the exact `RECV_ORD_ID_…` ack but only after a durable write so a failure lets 汇付 redeliver), `GET /payments/orders/{id}` (JWT poll, lazily expires a stale PENDING order).
- **Idempotency** (汇付 notify is at-least-once, 3× retry): the webhook `SELECT … FOR UPDATE`s the order, guards `PENDING → PAID` once, and `apply_topup`'s `idempotency_key="hf:"+out_trade_no` blocks a double credit. The webhook and the reconciler share `_topup_key` + `_grant_paid`, so the two settle paths can't double-grant even if both fire.
- **Reconciler** (`app/workers/payment_reconciler.py`): pull backstop for a lost notification, mirroring `settle_sweeper` (own `pg_try_advisory_xact_lock` key `b"PAYRECON"`, per-row sessions, folded into the same `python -m app.workers.run` process). Settles PENDING orders the provider reports paid; reaps expired ones.
- **Config** (`config.py`, all default empty/off so dev boots): `payment_enabled`, `huifu_merchant_id` / `huifu_sys_id` / `huifu_product_id` / `huifu_rsa_private_key` / `huifu_rsa_public_key` / `huifu_env` (`prod` | `mertest`) / `huifu_notify_url` (public HTTPS, includes the `/api/v1` root_path), `payment_min_cny` / `payment_max_cny` / `payment_order_ttl_seconds` / `payment_reconcile_interval_seconds`.

### Referral commission / withdrawal (`app/services/commission/`, `app/api/endpoints/commission.py`, `commission_ledger` + `withdrawal_request` tables)

A **separate, withdrawable cash wallet**, distinct from the credit ledger: when an invitee recharges, the inviter earns a commission (`commission_rate`, default 10% of the paid amount). This is real money (a **cash-payable** liability), so it is **not** mixed into `credit_ledger` (prepaid service liability) — it gets its own ledger in **RMB cents (整数分)** that structurally mirrors `credits/` (same `_internal` funnel, row-lock + flush, idempotency, double-entry invariant `user.commission_balance_fen == SUM(commission_ledger.amount_fen)`). `commission_ledger` writes funnel through `app/services/commission/_internal.py` only.

- **`commission_ledger`** (`app/models/commission_ledger.py`): append-only, BigInteger PK, signed `amount_fen`, `kind ∈ {COMMISSION, WITHDRAW, WITHDRAW_REFUND, CONVERT, CLAWBACK, ADJUST}`, `idempotency_key` UNIQUE. `available_at` is the **settlement hold** (set only on `COMMISSION` rows = `created_at + commission_hold_days`, default T+7) — a refund-clawback window. Withdrawable balance = `total − SUM(unmatured COMMISSION)`, computed read-time (`balance.lock_withdrawable_fen` under the user lock). **`withdrawal_request`** (`app/models/withdrawal_request.py`): UUID PK, cash-out requests; funds are debited up front (a `WITHDRAW` row, `ledger_id` back-link) so they can't be double-spent, mirroring `payment_order → credit_ledger`.
- **Accrual is decoupled from the buyer's top-up**: `commission.accrue_for_order(db, out_trade_no=…)` runs **best-effort post-commit** (own transaction, swallows failures) from both settle paths — the webhook endpoint and `reconcile.py` — *after* `apply_topup` commits, so a referrer-side failure can never roll back the buyer's recharge (same posture as `register.py`'s referral reward). Idempotent on `commission:{out_trade_no}`. Reads the referrer from `invite_redemption.referrer_user_id` (NULL for SYSTEM codes → no commission). **No new worker** — accrual is inline, payout is a script, the hold is read-time.
- **Two cash-out exits**: `POST /commissions/withdrawals` (cash payout, debits the wallet → PENDING `withdrawal_request`, operator settles out-of-band via `scripts/process_withdrawals.py --list|--pay|--reject`) and `POST /commissions/conversions` (convert commission → spendable credits at the `credit_price_cny` sale price; the one place two ledgers move together — a `CONVERT` debit + a `credits.apply_topup` in one transaction). Reads: `GET /commissions/balance` (total/frozen/withdrawable + rate), `GET /commissions/transactions`, `GET /commissions/withdrawals`. Money on the wire is yuan (`*_cny`); the service layer works in 分.
- **Config** (`config.py`, default off so dev boots): `commission_enabled`, `commission_rate` (0.1), `commission_hold_days` (7), `commission_min_withdraw_fen` (1000 = ¥10), `commission_min_convert_fen` (100 = ¥1). Errors: `commission_disabled` / `commission_amount_range` / `insufficient_commission` / `withdrawal_not_found`. **Out of scope (v1)**: auto 汇付 代付 payout, refund-webhook auto-CLAWBACK (manual via operator), per-invitee cap, and the frontend (邀请页/返佣余额/提现 UI).

### Skills (`app/api/endpoints/skill.py`, `app/services/skill_bundle.py`, `skill` + `user_skill_preference` tables)

A skill is an uploaded `SKILL.md` bundle (`.zip`, axon SKILL convention) the worker stages into the sandbox at session start so axon's `use_skill` tool can surface it. **Three origins** (`SkillOrigin`, replaced the old `BUILTIN` in commit `465bdba` — deploy the enum-swap migrations with that code):
- `SYSTEM` — always-on core skills the agent auto-selects. `user_id IS NULL`, **hidden from users** (never returned by `/api/skills`), agent/worker-only, operator-managed.
- `OFFICIAL` — platform-published skills a user browses and opts into. `user_id IS NULL`, operator/seed-managed.
- `CUSTOM` — user-uploaded, owner-scoped (`user_id` set), managed via `/api/skills`.

`user_id`/`origin` consistency is a DB `CheckConstraint` (`ck_skill_origin_user_id_consistent`): platform origins must have `user_id IS NULL`, CUSTOM must have an owner — so a CUSTOM bundle can't masquerade as a platform default. Platform-name uniqueness is the partial index `uq_skill_platform_name` (`WHERE user_id IS NULL`).

- **User-facing visibility is `OFFICIAL` + the caller's own `CUSTOM`** — `SYSTEM` is excluded from `list_skills`, `_get_visible_skill`, and the enqueue invocable filter (fixed in `e9d133b`; the old `PLATFORM_ORIGINS` set had leaked SYSTEM into the skills page and is removed — visibility ≠ platform-ownership). The worker/agent catalog still includes SYSTEM.
- **Upload** is one multipart POST; `skill_bundle.py` is the registration-time validator (rejects path traversal, symlinks, missing/unparseable `SKILL.md`, zip bombs) and extracts name + description from frontmatter, mirroring `axon.skills._parse_frontmatter` so backend-extracted metadata matches what the worker rediscovers. Bundles are **immutable** through this surface — editing means re-uploading (clean worker cache key). PATCH only touches picker metadata (name/description) and the enable toggle.
- **`display_name` i18n**: a bundle's `SKILL.md` `metadata.display_name` may be a plain string or a `{locale: name}` map. The map persists in `skill.display_name_i18n` (JSONB, `none_as_null` so "no map" is SQL `NULL`); `GET /skills?locale=` resolves the localized name via `app/services/skill_i18n.py` (the single parse/resolve/normalize chokepoint) with `display_name` as fallback. The map itself never leaves the backend; a PATCH rename clears it so explicit edits win. Backfill existing OFFICIAL rows with `scripts/backfill_official_skill_i18n.py`.
- **Enable toggle = ambient auto-discovery, not availability**: CUSTOM skills carry `enabled` on the row (owner flips directly). Platform rows are platform-wide, so a per-user toggle lives in `user_skill_preference` (`(user_id, skill_id)` → `enabled`), where effective enabled = `COALESCE(pref.enabled, skill.enabled)` — the expression shared by `GET /skills` and the worker's `list_enabled`. The toggle only decides whether the skill's metadata rides in the LLM context every turn (so the model can auto-invoke it); it does **not** gate explicit per-turn selection. Both origins ship **disabled** so enabling is always an explicit user opt-in (it costs ambient LLM context every turn): OFFICIAL via `scripts/upload_skill.py` deriving `enabled` from origin, CUSTOM via `register_skill_bundle` inserting `enabled=False`. Only SYSTEM ships enabled — the agent reads it through the same effective-enabled expression, so disabled would mean invisible to the agent. A freshly uploaded CUSTOM skill is usable immediately via an explicit composer pick (which ignores the toggle) even while disabled. Two per-user quotas, deliberately separate: uploads are capped at `skill_registration._MAX_SKILLS_PER_USER` (100, storage abuse bound) while *simultaneously enabled* skills are capped at `skill_enablement.MAX_ENABLED_SKILLS` (20, the ambient-context budget — enforced on the PATCH toggle with 403 `skill_enable_quota_exceeded`).
- **Per-turn selection**: `POST /sessions/{id}/messages` carries `skills: [name, ...]`. `queue_user_message._filter_invokable_skills` drops any the user can't see (someone else's CUSTOM, hidden SYSTEM) — an explicit pick is its own authorization, so the enable toggle is deliberately not consulted — and snapshots the visible rows (id/name/bundle_key) so the worker loads exactly what existed at enqueue time, request order preserved for the worker's reminder list.

### Recipes (`app/api/endpoints/recipe.py`, `app/models/recipe.py`, `recipe` table)

A recipe is a user-**selectable** scenario scaffold — picked from the composer (unlike a skill, which the agent chooses itself). Its `instructions` are injected into the turn as an axon `<system-reminder>` block (the *how*/steering, kept separate from the user's query — the *what*); the per-turn snapshot lives on `session_turn.recipe` and the worker injects it verbatim. Two origins (`RecipeOrigin`): `OFFICIAL` (platform-shipped, visible to all, `user_id IS NULL`) and `CUSTOM` (user-authored, owner-scoped) — **no `SYSTEM` tier**, because a recipe is always user-picked. Unlike an immutable skill bundle, a recipe's `instructions` are plain text with no staging step, so they're freely editable in place via `RecipeUpdate`.

### Daily check-in (`app/api/endpoints/check_in.py`, `app/services/check_in.py`, `check_in` table)

One credit reward per user per business-day (`settings.checkin_timezone`, Asia/Shanghai). `check_in` is append-only audit (`UNIQUE(user_id, check_in_date)` is the DB-enforced at-most-one-per-day invariant), `check_in_date` a `DATE` computed by the service in the business tz (not a UTC `now()` default). The reward amount is **not** stored on the row — it lives in the paired `CHECKIN` `credit_ledger` row (money lives in the ledger, same posture as `invite_redemption`), reached via `ledger_id`; the credit grant is idempotent behind that. `streak_day` is a write-time snapshot of the consecutive-day count so the "已连续签到 N 天" display never needs a gaps-and-islands scan. Config: `checkin_reward_credits`, `checkin_timezone`.

### Published sites (`app/models/session_site.py`, `session_site` table)

A static site published from a session via the agent's `publish_site` tool. The **worker** inserts one row per successful publish (after the files land in the OSS preview bucket), keyed by an unguessable 16-char base36 `slug` that doubles as the publish **subdomain label** and the **OSS key prefix** (`{slug}/...`). The public URL `https://{slug}.{preview_domain}/` is *derived* from `slug`, never stored, so a domain change can't strand a column. `user_id` is denormalized from the session owner (immutable) so "my published sites" is a single indexed scan. Backend owns the schema; the worker owns the rows (same split as `sandbox`/`session_context`).

### Organizations (`app/models/organization.py`)

Identity layer only, not yet the billing layer. Registration auto-creates a `PERSONAL` org per user (`OWNER` role). `TEAM` kind and `ADMIN`/`MEMBER` roles exist in the enums so future B2B multi-member work drops in without a migration, but are unused today — the credit ledger still attaches to `user_id`. Keep these two tables (`organization`, `organization_member`) narrow; new concerns (plans, SSO, avatars) get their own tables when actually needed.

### `message.blocks` shape

JSONB list of discriminated blocks. Block types: `text` (USER has `files[]`, ASSISTANT has plain text), `tool_use`, `tool_result` (paired by `tool_use_id`), `error`. Blocks are **immutable** after insert; there are no status/is_error discriminators — a `tool_use` without a matching `tool_result` means the tool never returned (cancelled, crashed, or session died). See `README.md` for the full schema and edge cases.

## Conventions

- Endpoint files keep inline DB queries; only non-trivial logic moves to `app/services/`. Session-service submodules (`enqueue`, `streaming`, `title`) are re-exported through `app.services.session.__init__` so callers write `from app.services.session import queue_user_message`. Internal helpers (`session/files.py`, `session/exceptions.py`) are not re-exported.
- `app.main` imports `setup_logging` **before** any other app imports so loguru captures module-level loggers.
- Pagination helper (`app/common/pagination.py`) clamps page/page_size to ≥1; list endpoints build one shared filter and apply it to both the count and row queries so totals can't drift from the page.
- **Connector vs MCP.** Two names for two things, and they do not overlap. A *Connector* is what the user configures and manages — it names the URL surface (`/connectors/*`), every request/response schema (`ConnectorCreate`, `ConnectorResponse`), every `error_type` the frontend localizes (`connector_*`), and everything the UI says. *MCP* is the protocol and the server on the other end — it names the client, the transport, the probe, and the OAuth machinery in `app/services/mcp/`. New code picks by which of the two it is about, not by which file it sits in. Two legacy exceptions, both named after the table rather than what they serve: the `mcp_server` / `mcp_server_catalog` tables (renaming a live table is not worth it) and `app/api/endpoints/mcp_server.py` with them. Neither is precedent — a new endpoint module serving Connectors is `connector*`.
- Exception handlers (`app/exception_handlers.py`) are registered for `AppError`, `IntegrityError`, `SQLAlchemyError`, `HTTPException`, `RequestValidationError`, and a catch-all `Exception`. Raise `AppError` subclasses (from `app/errors.py`) inside services rather than `HTTPException` — the handler converts them to a uniform JSON shape.

## Environment variables

Required in `backend/.env`: `db_uri` (PostgreSQL, `postgresql+psycopg://...`), `secret_key` (JWT), `redis_url`, `llm_base_url`, `llm_api_key`, `oss_access_key_id`, `oss_access_key_secret`. `session_tier_models` (maps the UI tier `FAST`/`STANDARD`/`ADVANCED` → the `model_id` written to `session.model_id`) and `supported_session_model_ids` (the whitelist the resolved `model_id` is checked against) have defaults in `config.py` — keep both in sync with `agent-worker/models.toml`, since a mismatch surfaces as a worker-side `ValueError` on the next turn rather than at session creation. SMTP creds (`smtp_*` — Lark Suite Mail by default, `smtp_host=smtp.larksuite.com`, port 465 SSL) and Aliyun SMS creds (`aliyun_sms_*` — `access_key_id`/`access_key_secret`/`sign_name`/`template_code`/`region`, for the phone-OTP channel) default to empty strings so dev boxes without provider access still boot.

MCP Connectors (`app/services/mcp/`, `app/api/endpoints/mcp_server.py`): `mcp_credential_encryption_key` (a Fernet key — generate with `Fernet.generate_key()`) encrypts Connector credentials and OAuth tokens at rest and **must be byte-identical to the worker's `mcp_credential_encryption_key`**, or the worker can't decrypt what the backend sealed. Empty default still boots, but create/probe fails loudly the moment a credential needs sealing (any BEARER/HEADER/OAUTH Connector). OAuth Connectors additionally need `mcp_oauth_redirect_uri` — the public `{app origin}/api/connectors/oauth/callback` (the frontend serves that proxy route; register it with manual OAuth apps) — empty ⇒ the authorize step fails with a 500 `oauth_not_configured` and a log line naming the setting, while none/bearer/header Connectors keep working; `mcp_oauth_state_ttl_seconds` (default 600) bounds the pending-authorization Redis state. `mcp_client_metadata_url` (optional) enables **CIMD** — the URL itself becomes our `client_id` and the authorization server fetches it, so no per-Connector registration is stored; it must be https with a non-root path and must resolve to this API's `GET /api/connectors/client-metadata` (public + rate-limit exempt, since the fetch comes from the provider). Unset ⇒ dynamic client registration, which stays the fallback for servers that don't advertise CIMD. Ships ten `mcp_server` alembic migrations (plus the `session_turn` snapshot one) — run `alembic upgrade head` before serving, and before deploying the worker, which reads and writes the whole grant. **This module obtains grants; it does not renew them.** Renewal is the worker's, on demand during a turn (`agent-worker/agent_worker/infra/connector_grant_rotation.py`), because a turn can outlive its access token — Atlassian issues one-hour tokens against a one-hour `max_execution_time` — and no look-ahead window closes that gap. A refresh token is single-use, so exactly one party may spend it; what guarantees that across N worker replicas is a claim written on `oauth_refresh_after`, not there being a single writer process. What stays here is the browser flow: authorize, exchange, and the RFC 7009 revocation that ends a grant. **Disconnecting an OAuth Connector gives the grant up rather than shelving it**: the tokens are cleared and an RFC 7009 revocation is sent to the provider after the response (best-effort — our copy is gone either way), so reconnecting means authorizing again. What survives is the row and its client registration, which is the whole difference from deleting it; the other auth types keep their credential, because the user typed it and it is not ours to throw away. Renewing a grant for a disconnected row would be maintaining access the user believes they ended, so the worker's claim query filters on `connected` too.

Credit/quota knobs (all defaulted in `config.py`, edit-and-restart for V1): `credit_rates` (per-model rate card — one entry per `supported_session_model_ids` model, else reserve fails for that model), `credit_per_second_credits` (β infra fee), `credit_reserve_max_turn_seconds` / `credit_reserve_max_input_tokens` (pessimistic reserve-time upper bounds; settle uses actuals), `credit_welcome_grant_credits` (auto-grant on registration), `max_concurrent_running_tasks` (per-user RUNNING-session cap, default 10), `checkin_reward_credits` / `checkin_timezone` (daily check-in). Skills use `oss_skill_bucket_name` (separate OSS bucket for bundles). See `config.py` for the full `Settings` model.
