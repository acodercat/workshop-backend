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

# Production — single-host manual deploy via scripts/deploy.sh
bash scripts/deploy.sh deploy       # stop + uv sync + alembic upgrade + start
bash scripts/deploy.sh restart      # code-only restart (no deps / no migration)
bash scripts/deploy.sh status       # is :8005 bound? by whom?
bash scripts/deploy.sh logs         # tail -f logs/uvicorn.log
# Truth source is ``lsof -t -i:8005``, not a PID file, so it tolerates
# instances started outside the script. uvicorn stdout → logs/uvicorn.log;
# loguru/stdlib app logs go elsewhere under logs/ per app/logging_config.py.

# HTTPS / custom port — script's defaults won't fit; use uvicorn directly:
uv run uvicorn app.main:app --host 0.0.0.0 --port 9090 --workers 1 \
  --ssl-certfile ./cert/<domain>.pem --ssl-keyfile ./cert/<domain>.key

# Migrations
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "description"

# Tests (pytest; asyncio_mode is project-level "auto" — no @pytest.mark.asyncio needed)
uv run pytest tests/                       # currently only tests/e2e/ — there is no unit suite
uv run pytest tests/e2e/test_session_e2e.py::test_name

# Full-pipeline E2E (opt-in; needs backend + worker + Redis + Postgres + sandbox running)
RUN_E2E_TESTS=1 uv run pytest tests/e2e/ -v
```

`alembic/env.py` imports `app.models.*` and `config.settings` — migrations rely on `db_uri` from `.env` at repo root.

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
- Worker's `models.toml` has the model id the test creates sessions
  with — defaults to `settings.default_session_model_id`
  (`claude-opus-4-6`); change in the test if your env doesn't carry it.

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

The backend and worker **never talk directly**. Redis is the sole bridge:

- Backend pushes user turns onto `session_queue` (Stream) and publishes on `notify:worker` (Pub/Sub) to wake the worker.
- Worker XADDs events to `session_stream:{session_id}` (Stream).
- Backend's `GET /sessions/{id}/stream` XRANGEs (replay) then XREADs (follow) that stream and forwards each entry as SSE.

Redis key names (`SESSION_QUEUE`, `SESSION_STREAM_PREFIX`, `WORKER_NOTIFY_CHANNEL`) in `app/services/redis.py` are a **wire contract** mirrored in `agent-worker/agent_worker/queue.py`. Changing one without the other silently desyncs the services.

### State split: DB vs worker

- **DB is rendering state.** `message.blocks` (JSONB) holds the structured block list the frontend renders. USER rows are inserted in `queue_user_message`; ASSISTANT rows are projected from worker events (the worker writes them — backend does not synthesize assistant rows).
- **Worker owns LLM state.** Conversation history / compacted context lives on the worker side. The backend enqueues only `{session_id, query, files}` — it deliberately does **not** send history.
- `session.status ∈ {running, complete, failed}` is set by the backend the moment a turn is enqueued (`running`), and flipped to a terminal value by the worker atomically with the assistant insert. The frontend uses this to decide whether to open the SSE stream.
- `session.model_id` is stamped at session creation from `settings.default_session_model_id` and never mutated. It's an audit field — the worker selects its actual LLM via its own config, not from this column. If we later want per-session model selection, `model_id` is the join point: have the worker read `session.model_id` instead of (or as override over) its own `llm_model`.

### SSE streaming (`app/services/session/streaming.py`)

Two-phase generator: replay all existing stream entries, then block-read until an `agent.stopped` terminal event arrives. An older version merged `message_chunk` events into one blob on replay and lost tool calls — do not reintroduce that. Cold history is served by `GET /sessions/{id}` (structured `blocks`), not by the SSE endpoint; the stream endpoint is only meaningful while `status == "running"`.

### Redis client setup (`app/main.py` lifespan)

Two pools, intentionally separate (see `_make_redis_client`):

- **Short-op pool** (`app.state.redis`) — `BlockingConnectionPool` sized by `redis_pool_size` (default 30). XADD, PUBLISH, DELETE, PING. When full, callers wait up to `redis_pool_timeout_seconds` for a connection before raising — trades a tail-latency spike for fewer 5xx during bursts.
- **Streaming pool** (`app.state.redis_streaming`) — plain `ConnectionPool` sized by `redis_streaming_pool_size` (default 100). Holds one connection per active SSE for the full XREAD `block_ms`. Fail-fast on full, because hitting the cap means the box is at its SSE capacity — back-pressure is the right answer.

Sharing a single pool would let SSE long-polls starve short-op traffic. Both pools probe checked-out connections on the 30s `health_check_interval` so a silently-dropped socket surfaces promptly.

Startup uses `_ping_db` / `_ping_redis` with exponential backoff (`startup_probe_*` settings) so a few-second dependency race at boot doesn't crash-loop the pod. Liveness (`/health/live`) is always 200; readiness (`/health/ready`) returns 503 when DB or Redis are unreachable so K8s stops routing to the pod without restarting it.

### Enqueue ordering (`app/services/session/enqueue.py`)

`queue_user_message` has a load-bearing sequence:
1. Reject (409) if `session_stream:{id}` has a non-terminal tail (generation in progress). If the tail is `agent.stopped`, delete the stream to start clean.
2. INSERT user message + flip `status = running` in a single commit.
3. XADD to `session_queue` + publish on `notify:worker`. On failure, roll status back to `complete` and raise 503.

### Events wire format (`app/common/event_types.py`, `app/common/sse.py`)

`EventType` enum is the backend's independent copy of the strings the worker emits (`agent_worker/emitter.py` is the authoritative producer). The backend has no runtime dependency on axon; it only consumes the stream. Redis Streams store `str → str` only, so structured fields (`args`, `files`, `usage`) arrive JSON-encoded and are forwarded **verbatim** to the frontend. `null`/`None` lands on the wire as the literal string `"null"`.

### Auth

JWT (`app/auth.py`), bcrypt passwords, phone-as-username. Revocation is by bumping `user.token_version` — tokens carry the version they were issued against; `_decode_and_get_user` rejects tokens whose version is stale. `revoke_user_tokens` is called after password change. SSE endpoints use `validate_token` with a query-param token because `EventSource` cannot set headers.

### Registration / email verification (`app/services/auth/`, `app/services/email/`)

There is no open `POST /register`. The flow is: `POST /auth/send-register-code` → email containing a 6-digit code via SMTP (Lark Suite Mail, transport in `app/services/email/client.py` via `aiosmtplib`) → `POST /auth/register` with `email + code + password`. State for in-flight codes lives in Redis (TTL = `register_code_ttl_seconds`, default 600). Per-email resend cooldown (`register_code_resend_cooldown_seconds`, default 60) and per-code wrong-attempt cap (`register_code_max_attempts`, default 5) are layered with per-IP rate limits on the endpoints themselves. SMTP creds default to empty so dev boxes without SMTP access still boot — calls that need SMTP fail loudly at send time, not at startup. Upstream Lark Suite caps (200/100s, 6000/day per sender) are well above the rate-limit envelope, so no app-level throttling is needed. The `email_change.py` service reuses the same code-verification primitive for changing a user's email.

### Rate limiting (`app/rate_limit.py`)

slowapi, keyed on client IP. Limits are config knobs (`rate_limit_*` in `config.py`): `login` 5/min, `message_create` 30/min, `share_unlock` 5/5min, `register_code` 5/hour, `register` 20/hour, default 200/min. `RateLimitExceeded` is registered as an exception handler in `app/main.py`. If abuse appears, raise the limits behind authenticated identity rather than the per-IP key (NAT'd corp networks otherwise hit the cap collectively).

### Sharing (`app/api/endpoints/share.py`, `session_share` table)

A session owner mints a share record with a password. Recipients hit a public unlock endpoint that swaps the password for a short-TTL **share-view token** (`share_view_token_expire_minutes`, default 15) — a JWT bound to that share, distinct from a normal user access token. The session's blocks are served read-only against that token. Short TTL because the recipient is actively viewing; re-prompting after idle limits the blast radius of a leaked URL.

### Feedback (`app/api/endpoints/feedback.py`)

Two tables, two shapes. `user_feedback` is freeform product feedback. `message_feedback` is per-assistant-message thumbs/comment with a unique `(user_id, message_id)` constraint enforced via `IntegrityError → 409` in the handler.

### File storage (`app/services/file_storage.py`)

Frontend uploads/downloads directly to/from Alibaba OSS via presigned URLs. The backend **never proxies file bytes** — `FileStorage` only mints URLs. Upload URLs validate the extension against `_DOCUMENT_CONTENT_TYPES`; download URLs embed a ``response-content-disposition`` override so the cross-origin OSS response forces a save-as with the real filename. Authorization is JWT-only: the file_key itself is UUID-backed and unguessable, and the presigned URL carries its own short TTL — no per-key ownership table.

### `message.blocks` shape

JSONB list of discriminated blocks. Block types: `text` (USER has `files[]`, ASSISTANT has plain text), `tool_use`, `tool_result` (paired by `tool_use_id`), `error`. Blocks are **immutable** after insert; there are no status/is_error discriminators — a `tool_use` without a matching `tool_result` means the tool never returned (cancelled, crashed, or session died). See `README.md` for the full schema and edge cases.

## Conventions

- Endpoint files keep inline DB queries; only non-trivial logic moves to `app/services/`. Session-service submodules (`enqueue`, `streaming`, `title`) are re-exported through `app.services.session.__init__` so callers write `from app.services.session import queue_user_message`. Internal helpers (`session/files.py`, `session/exceptions.py`) are not re-exported.
- `app.main` imports `setup_logging` **before** any other app imports so loguru captures module-level loggers.
- Pagination helper (`app/common/pagination.py`) clamps page/page_size to ≥1; list endpoints build one shared filter and apply it to both the count and row queries so totals can't drift from the page.
- Exception handlers (`app/exception_handlers.py`) are registered for `AppError`, `IntegrityError`, `SQLAlchemyError`, `HTTPException`, `RequestValidationError`, and a catch-all `Exception`. Raise `AppError` subclasses (from `app/errors.py`) inside services rather than `HTTPException` — the handler converts them to a uniform JSON shape.

## Environment variables

Required in `backend/.env`: `db_uri` (PostgreSQL, `postgresql+psycopg://...`), `secret_key` (JWT), `redis_url`, `llm_base_url`, `llm_api_key`, `oss_access_key_id`, `oss_access_key_secret`. `default_session_model_id` (the value written to `session.model_id` on creation) and `supported_session_model_ids` (the whitelist `POST /sessions` validates against) have defaults in `config.py` — keep `supported_session_model_ids` in sync with `agent-worker/models.toml`, since a mismatch surfaces as a worker-side `ValueError` on the next turn rather than at session creation. SMTP creds (`smtp_*`) default to empty strings so dev boxes without SMTP access still boot — Lark Suite Mail is the default provider (`smtp_host=smtp.larksuite.com`, port 465 SSL). See `config.py` for the full `Settings` model.
