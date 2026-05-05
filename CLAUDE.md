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

# Production
uv run uvicorn app.main:app --host 0.0.0.0 --port 8005 --workers 4

# Migrations
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "description"

# Tests (pytest; asyncio_mode is project-level "auto" — no @pytest.mark.asyncio needed)
uv run pytest tests/
uv run pytest tests/test_file.py::test_name

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

### SSE streaming (`app/services/sessions/streaming.py`)

Two-phase generator: replay all existing stream entries, then block-read until an `agent.stopped` terminal event arrives. An older version merged `message_chunk` events into one blob on replay and lost tool calls — do not reintroduce that. Cold history is served by `GET /sessions/{id}` (structured `blocks`), not by the SSE endpoint; the stream endpoint is only meaningful while `status == "running"`.

### Enqueue ordering (`app/services/sessions/enqueue.py`)

`queue_user_message` has a load-bearing sequence:
1. Reject (409) if `session_stream:{id}` has a non-terminal tail (generation in progress). If the tail is `agent.stopped`, delete the stream to start clean.
2. INSERT user message + flip `status = running` in a single commit.
3. XADD to `session_queue` + publish on `notify:worker`. On failure, roll status back to `complete` and raise 503.

### Events wire format (`app/common/event_types.py`, `app/common/sse.py`)

`EventType` enum is the backend's independent copy of the strings the worker emits (`agent_worker/emitter.py` is the authoritative producer). The backend has no runtime dependency on axon; it only consumes the stream. Redis Streams store `str → str` only, so structured fields (`args`, `files`, `usage`) arrive JSON-encoded and are forwarded **verbatim** to the frontend. `null`/`None` lands on the wire as the literal string `"null"`.

### Auth

JWT (`app/auth.py`), bcrypt passwords, phone-as-username. Revocation is by bumping `user.token_version` — tokens carry the version they were issued against; `_decode_and_get_user` rejects tokens whose version is stale. `revoke_user_tokens` is called after password change. SSE endpoints use `validate_token` with a query-param token because `EventSource` cannot set headers.

### File storage (`app/services/file_storage.py`)

Frontend uploads/downloads directly to/from Alibaba OSS via presigned URLs. The backend **never proxies file bytes** — `FileStorage` only mints URLs. Upload URLs validate the extension against `_DOCUMENT_CONTENT_TYPES`; download URLs embed a ``response-content-disposition`` override so the cross-origin OSS response forces a save-as with the real filename. Authorization is JWT-only: the file_key itself is UUID-backed and unguessable, and the presigned URL carries its own short TTL — no per-key ownership table.

### `message.blocks` shape

JSONB list of discriminated blocks. Block types: `text` (USER has `files[]`, ASSISTANT has plain text), `tool_use`, `tool_result` (paired by `tool_use_id`), `error`. Blocks are **immutable** after insert; there are no status/is_error discriminators — a `tool_use` without a matching `tool_result` means the tool never returned (cancelled, crashed, or session died). See `README.md` for the full schema and edge cases.

## Conventions

- Endpoint files keep inline DB queries; only non-trivial logic moves to `app/services/`. The two session-service submodules (`enqueue`, `streaming`) are re-exported through `app.services.session.__init__` so callers write `from app.services.session import queue_user_message`.
- `app.main` imports `setup_logging` **before** any other app imports so loguru captures module-level loggers.
- `FRONTEND_DIR` mount (`app/main.py`) serves the built frontend at `/static` when `frontend/dist/` exists — expected in production single-container deployments.
- Pagination helper (`app/common/pagination.py`) clamps page/page_size to ≥1; list endpoints build one shared filter and apply it to both the count and row queries so totals can't drift from the page.

## Environment variables

Required in `backend/.env`: `db_uri` (PostgreSQL, `postgresql+psycopg://...`), `secret_key` (JWT), `redis_url`. OSS credentials and `default_session_model_id` (the value written to `session.model_id` on creation) have defaults in `config.py`. See `config.py` for the full `Settings` model.
