# FastAPI Backend Boilerplate

Base project template with async SQLAlchemy, JWT auth, and Alembic migrations.

## Setup

```bash
# Install dependencies
uv sync

# Configure environment variables
cp .env.example .env
# Edit .env with your database URI and secret key

# Run database migrations
alembic upgrade head

# Start development server
uvicorn app.main:app --host 0.0.0.0 --port 8005 --reload
```

## Production

Single-host manual deploy: use `scripts/deploy.sh`. Two siblings are
managed by the same script — **web** (uvicorn on `:8005`) and
**background-settle** (sweeper + subscriber, see below). Subcommand
layout mirrors the frontend's `serve.sh` so muscle memory carries
across services.

```bash
# After ``git pull`` (handles deps + migrations + restart of both)
bash scripts/deploy.sh deploy

# Web-only commands
bash scripts/deploy.sh restart       # code-only restart (no deps/migration)
bash scripts/deploy.sh logs          # tail uvicorn.log
bash scripts/deploy.sh status        # show state of BOTH web + background-settle

# background-settle-only commands
bash scripts/deploy.sh settle start
bash scripts/deploy.sh settle stop
bash scripts/deploy.sh settle restart
bash scripts/deploy.sh settle status
bash scripts/deploy.sh settle logs   # tail settle.log
```

| command | behavior |
|---|---|
| `start` | start uvicorn on `:8005` (no-op if already running) |
| `stop` | SIGTERM the web, escalate to SIGKILL after a 10s grace period |
| `restart` | `stop` + `start` — no deps / migration sync |
| `status` | report state of **both** web (`:8005`) and background-settle |
| `logs` | `tail -f logs/uvicorn.log` |
| `deploy` | stop both + `uv sync` + `alembic upgrade head` + start both |
| `settle {start\|stop\|restart\|status\|logs}` | same lifecycle commands for the background-settle process (`logs/settle.log`) |

Truth source for "is it running":
- **web**: `lsof -t -i:8005` — tolerates instances started outside the
  script (e.g., a legacy `nohup uv run uvicorn …`).
- **background-settle**: `pgrep -f 'app.workers.run'` — no port; the
  module path is specific enough not to collide.

uvicorn stdout → `logs/uvicorn.log`; background-settle stdout →
`logs/settle.log`; application-level loguru output lives elsewhere
under `logs/` per `app/logging_config.py`.

For ad-hoc / non-default-port runs (or when the script's defaults
don't fit, e.g. a custom SSL setup), the bare commands still work:

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8005 --workers 4
uv run python -m app.workers.run          # standalone background-settle
```

### Background-settle (sweeper + subscriber)

Closes out **credit reservations** that the user-facing lazy sweep
didn't catch — the "user never came back" case for abandoned
sessions, API-only clients, and worker crashes between RESERVE and
the fenced terminal write. One process, two concurrent tasks (see
`app/workers/run.py`):

- `app/workers/settle_sweeper.py` — periodic 60s poll over OPEN
  reservations. Correctness backstop, coordinated across hosts via a
  Postgres advisory lock so only one node sweeps per tick.
- `app/workers/settle_subscriber.py` — Redis `settle:notify` pub/sub
  subscriber. The worker publishes the session_id immediately after
  committing a terminal `session_turn`, so settle happens within
  sub-second instead of waiting for the next sweep tick.

The two are push and pull halves of the same settle responsibility —
the subscriber is a latency optimization, the sweeper is the
correctness path. Both call the same shared helpers
(`settle_open_for_session` / `refund_reservation`); behaviour stays
identical to the on-endpoint lazy sweep.

**Multi-node deployment**: every host should run a background-settle.
Sweepers coordinate across the cluster via the Postgres advisory
lock (`pg_try_advisory_lock`); at each tick only one host actually
scans, the others see a busy lock and skip. Connection close
auto-releases the lock, so a crashed process never strands it.
The subscriber broadcasts to every host's listener; concurrent
settles serialize on the reservation's `FOR UPDATE` lock and the
state-machine no-op handles the loser.

```bash
# Operationally — same lifecycle commands as web
bash scripts/deploy.sh settle start
bash scripts/deploy.sh settle status
bash scripts/deploy.sh settle logs
```

The sweeper polls every 60 s by default (see
`settle_sweeper.run_forever`), processes at most 100 reservations
per tick (capped to keep the advisory-lock hold time short), and
exponentially backs off on consecutive failures.

### Crediting users (admin top-up)

Every new registration automatically receives the welcome grant
configured by `settings.credit_welcome_grant_credits` (default
100,000 credits, set to 0 to disable). For everything else — refunds,
business deals, accounts that pre-date the welcome grant — use
`scripts/topup_credits.py`:

```bash
uv run python -m scripts.topup_credits \
    --user-id=42 \
    --credits=10000 \
    --idempotency-key=manual-2026-05-15-acme \
    --reason="prepaid contract Q2"
```

- `--credits` is whole credits (1 credit ≈ \$0.001 USD of LLM cost).
- `--idempotency-key` is required and globally unique per logical
  top-up. Re-running with the same key after a success exits
  cleanly with "already applied" — safe to retry.
- The script writes one `TOPUP` row to `credit_ledger` and bumps
  `user.credit_balance` atomically. All credit-balance writes funnel
  through this same code path (see `app/services/credits/_internal.py`).

For signed corrections (clawbacks / promo credits / mistake fixes),
import `app.services.credits.apply_adjust` directly — a CLI for it
ships when there's a real ops need.

## Project Structure

```
app/
├── main.py                 # FastAPI app entry point
├── database.py             # Async SQLAlchemy engine and session
├── auth.py                 # JWT authentication and password hashing
├── logging_config.py       # Loguru logging setup
├── exception_handlers.py   # Global exception handlers
├── api/
│   ├── dependencies.py     # Shared dependencies (db session)
│   └── endpoints/          # Route handlers
├── models/                 # SQLAlchemy ORM models
├── schemas/                # Pydantic request/response schemas
├── services/               # Business logic layer
├── tasks/                  # Background async tasks
├── common/                 # Shared utilities
└── prompts/                # Prompt templates
config.py                   # Application settings (pydantic-settings)
alembic/                    # Database migrations
```

## Database Migrations

```bash
# Create a new migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head
```

## `message.blocks` shape

The `message.blocks` column (JSONB) stores a list of UI blocks for
rendering a turn. USER rows are written by `create_message`; ASSISTANT
rows are written by the worker via `projection.project_turn`. The
frontend reducer should handle the full block-type union below.

**USER message** — a single text block carrying the message and any
uploaded files inline:

```json
[
    {
        "type": "text",
        "content": "Analyze this financial report for me",
        "files": [
            {
                "file_key": "documents/a1b2c3d4-.../financials.pdf",
                "filename": "financials.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 2457892
            }
        ]
    }
]
```

`files` is always present on a USER text block (possibly empty `[]`).

**ASSISTANT message** — a turn where the agent read the file, ran some
code, produced a report, and delivered it back. Tool calls appear as
**pairs** of `tool_use` and `tool_result` blocks linked by
`tool_use_id`:

```json
[
    {"type": "text", "content": "Sure — I'll start by reading the file."},
    {
        "type": "tool_use",
        "tool_use_id": "call_ab12",
        "tool": "read_file",
        "args": {"path": "/workspace/inputs/financials.pdf"}
    },
    {
        "type": "tool_result",
        "tool_use_id": "call_ab12",
        "tool": "read_file",
        "output": "Q1 Revenue: 12.4M\nQ2 Revenue: 14.1M\n...",
        "files": []
    },
    {
        "type": "tool_use",
        "tool_use_id": "call_cd34",
        "tool": "run_code",
        "args": {"code": "import pandas as pd\ndf = pd.read_csv(...)\n..."}
    },
    {
        "type": "tool_result",
        "tool_use_id": "call_cd34",
        "tool": "run_code",
        "output": "Revenue YoY: +15.3%\nCost YoY: +8.2%\nNet margin: +2.1pp",
        "files": []
    },
    {"type": "text", "content": "Analysis complete:\n- Revenue up 15.3% YoY\n- Net margin improved by 2.1pp..."},
    {
        "type": "tool_use",
        "tool_use_id": "call_ef56",
        "tool": "deliver_file",
        "args": {"path": "/workspace/outputs/report.md"}
    },
    {
        "type": "tool_result",
        "tool_use_id": "call_ef56",
        "tool": "deliver_file",
        "output": "File delivered: report.md (3,241 bytes)",
        "files": [
            {
                "file_key": "outputs/sess-123/.../report.md",
                "filename": "report.md",
                "mime_type": "text/markdown",
                "size_bytes": 3241
            }
        ]
    }
]
```

### Block types

| type | Role | Fields | Notes |
|---|---|---|---|
| `text` (USER) | USER | `content`, `files` | `files` is always a list of file refs (possibly empty); frontend downloads via `GET /files/download-url?file_key=…` |
| `text` (ASSISTANT) | ASSISTANT | `content` | Plain text (may contain Markdown). No `files`. |
| `tool_use` | ASSISTANT | `tool_use_id`, `tool`, `args` | One per tool invocation. Immutable once written. |
| `tool_result` | ASSISTANT | `tool_use_id`, `tool`, `output`, `files` | Paired with a prior `tool_use` via matching `tool_use_id`. `files` is always present (possibly empty). |
| `error` | ASSISTANT (failure path) | `message` | Terminal block when the agent raised before completing. |

**Lifecycle semantics** — blocks are immutable after creation and
there are no discriminator fields (no `status`, no `is_error`). A
`tool_use` with a matching `tool_result` = tool finished; a `tool_use`
without one = tool never returned (cancelled, errored mid-flight, or
the session itself died). Error text from an errored tool, if any,
lives in `tool_result.output`.

Both directions of `files` use the **same** file-ref shape:
`{file_key, filename, mime_type, size_bytes}`. Frontend download flow
is identical — USER uploads and ASSISTANT outputs both go through
presigned OSS URLs.

### Edge cases

- **Agent failure mid-turn** — `blocks` ends with an `error` block and
  `session.status = 'failed'`. The last `tool_use` block has no matching
  `tool_result` because it never arrived.
- **Tool returned error** — a normal `tool_result` block with the error
  text in `output`. Nothing on the block marks it as an error; the
  distinction is in the text itself. The turn can still finish as
  `complete`.
- **User cancellation** — indistinguishable from "tool never returned"
  in the persisted timeline: the `tool_use` block sits without a
  `tool_result`, same shape as a tool that crashed or ran out of time.
- **Empty turn** — if the agent hits `max_turns` without producing any
  chunk or tool call, `blocks` is `[]`. This is valid.


uv run uvicorn app.main:app --host 0.0.0.0 --port 9090 --workers 1 \
  --ssl-certfile ./cert/api.hebei.metaseq.com.cn.pem \
  --ssl-keyfile ./cert/api.hebei.metaseq.com.cn.key
