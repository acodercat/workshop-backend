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

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8005 --workers 4
```

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