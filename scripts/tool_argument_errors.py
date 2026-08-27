"""How often a model's tool arguments fail the tool's schema, per model × tool.

Why this exists
---------------
When the model emits arguments the tool's Pydantic schema rejects (a
number quoted as a string, a renamed field, a whole object serialized
into one field), axon answers with an ``is_error`` tool_result reading
``Invalid arguments — …`` and the model retries next turn. That retry
re-sends the whole context — one full model call spent on a formality.
A pre-validation shim on the tool (repair the known shapes, then
validate) would save that call, but only earns its place if the errors
are frequent and concentrated. This script gives the number, and says
which tools and which models.

What it computes, per model and tool
------------------------------------
- calls          : tool_result blocks in the window
- invalid_args   : the schema rejected the shape — ``Invalid arguments — …``
                   other than the malformed-JSON placeholder. A shim could
                   fix these.
- malformed_json : ``Invalid arguments — malformed JSON`` — the streamed
                   arguments never parsed as JSON at all (cut at the length
                   limit, or broken escaping). Counted apart: no shim
                   repairs it.
- rate           : invalid_args / calls

Source is ``message.blocks`` — the worker persists every tool_result
there, and neither axon nor the worker logs these errors, so the logs
cannot answer this. Soft-deleted sessions are included; the model still
made the call.

Usage:
    uv run python -m scripts.tool_argument_errors --days=30
    uv run python -m scripts.tool_argument_errors --days=7 --show-all   # tools with no errors too

Read-only: issues a single SELECT, writes nothing.
"""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.database import AsyncSessionLocal

# The two results axon emits (``axon._placeholders``): ``INVALID_ARGS_TEMPLATE``
# is ``"Invalid arguments — {error}"``, matched as a LIKE pattern, and
# ``MALFORMED_TOOL_ARGS_PLACEHOLDER`` is the fixed string. Both are only ever
# emitted with ``is_error=True``; the query checks the flag as a guard.
_INVALID_ARGS_PATTERN = "Invalid arguments — %"
_MALFORMED_JSON = "Invalid arguments — malformed JSON"

_QUERY = text(
    """
    SELECT s.model_id,
           b->>'tool' AS tool,
           count(*) AS calls,
           count(*) FILTER (
               WHERE (b->>'is_error')::boolean
                 AND b->>'output' LIKE :invalid_args_pattern
                 AND b->>'output' <> :malformed_json
           ) AS invalid_args,
           count(*) FILTER (
               WHERE (b->>'is_error')::boolean
                 AND b->>'output' = :malformed_json
           ) AS malformed_json
    FROM message m
    JOIN session s ON s.id = m.session_id
    CROSS JOIN LATERAL jsonb_array_elements(m.blocks) AS b
    WHERE m.created_at >= :since
      AND b->>'type' = 'tool_result'
    GROUP BY s.model_id, b->>'tool'
    ORDER BY s.model_id, invalid_args DESC, calls DESC
    """
)

# (model_id, tool, calls, invalid_args, malformed_json); ``tool`` is NULL
# for a block that carries no tool name.
_Counts = tuple[str, str | None, int, int, int]


async def _load(since: datetime) -> list[_Counts]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            _QUERY,
            {
                "since": since,
                "invalid_args_pattern": _INVALID_ARGS_PATTERN,
                "malformed_json": _MALFORMED_JSON,
            },
        )
        return [tuple(row) for row in rows.all()]


def _rate(part: int, whole: int) -> str:
    return f"{part / whole:.2%}" if whole else "n/a"


def _report(rows: list[_Counts], *, show_all: bool) -> None:
    by_model: dict[str, list[_Counts]] = {}
    for row in rows:
        by_model.setdefault(row[0], []).append(row)

    for model_id, tools in by_model.items():
        calls = sum(t[2] for t in tools)
        invalid = sum(t[3] for t in tools)
        malformed = sum(t[4] for t in tools)
        print(
            f"\n── {model_id}  (calls={calls:,}, invalid={invalid:,} = {_rate(invalid, calls)}, "
            f"malformed={malformed:,})"
        )
        print(f"   {'tool':<28} {'calls':>8} {'invalid':>8} {'rate':>7} {'malformed':>10}")
        for _, tool, tool_calls, tool_invalid, tool_malformed in tools:
            if not show_all and not (tool_invalid or tool_malformed):
                continue
            print(
                f"   {tool or '(none)':<28} {tool_calls:>8,} {tool_invalid:>8,} "
                f"{_rate(tool_invalid, tool_calls):>7} {tool_malformed:>10,}"
            )


async def main(args: argparse.Namespace) -> int:
    since = datetime.now(UTC) - timedelta(days=args.days)
    rows = await _load(since)
    print(f"Tool results since {since:%Y-%m-%d} ({args.days} days)")
    if not rows:
        print("No tool results in the window.")
        return 0
    _report(rows, show_all=args.show_all)
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--days", type=int, default=30, help="Look-back window (default 30).")
    p.add_argument(
        "--show-all",
        action="store_true",
        help="List every tool, not only those with errors.",
    )
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(_parse_args())))
