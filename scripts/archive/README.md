# Archived one-off scripts

Completed backfills, migrations, and incident fixes, kept for reference —
each ran against a schema or data state that has since moved on, so **read a
script against the current models before re-running it**. Everything still in
`scripts/` proper is live operator tooling, safe to run as documented.

Scripts here still run as modules (`uv run python -m scripts.archive.<name>`);
each file's own docstring says what it did and when re-running is a no-op.
