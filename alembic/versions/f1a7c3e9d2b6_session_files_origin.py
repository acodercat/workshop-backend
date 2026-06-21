"""session_files.origin: add Postgres enum column

Adds ``origin`` to ``session_files`` so the cross-session file library
(``GET /files``) can list user-facing files (``UPLOAD`` + ``DELIVERABLE``)
and hide agent-side artifacts (``INTERNAL`` — ``read_image`` inputs the
model looked at, the office→PDF ``{name}.preview.pdf`` sidecars). Mirrors
the project enum convention (``skill_origin`` / ``user_status``): a typed
Postgres enum with UPPERCASE labels matching the Python member names.

Going forward the producer stamps each row: the worker tags by bind path
(output → DELIVERABLE, input + preview keys → INTERNAL — provider-agnostic,
no key parsing), the backend's upload path tags UPLOAD.

Existing rows are backfilled here from the OSS key shape — exact for the
OpenAI-style ``functions.<tool>`` keys. Old Bedrock ``toolu_`` outputs
carry no tool name in the key, so their ``read_image`` inputs can't be
told apart and fall to DELIVERABLE: harmless (a few extra images in the
library for pre-migration Bedrock sessions only — new rows are tagged
correctly by the worker).

``server_default 'DELIVERABLE'`` keeps the column safe across the
backend→worker deploy gap: an old worker still INSERTing without
``origin`` lands on DELIVERABLE (shown) instead of crashing on NOT NULL.

Revision ID: f1a7c3e9d2b6
Revises: c7d2f9a4e1b8
Create Date: 2026-06-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f1a7c3e9d2b6"
down_revision: Union[str, Sequence[str], None] = "c7d2f9a4e1b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VALUES = ("UPLOAD", "DELIVERABLE", "INTERNAL")
_ENUM_NAME = "session_file_origin"


def upgrade() -> None:
    op.execute(
        f"CREATE TYPE {_ENUM_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _VALUES)})"
    )
    op.add_column(
        "session_files",
        sa.Column(
            "origin",
            sa.Enum(*_VALUES, name=_ENUM_NAME, create_type=False),
            nullable=False,
            server_default=sa.text(f"'DELIVERABLE'::{_ENUM_NAME}"),
        ),
    )
    # Backfill from the OSS key shape. Order matters: ``.preview.pdf``
    # sidecars live *under* the deliver_file tool path, so they must be
    # matched before the deliver_file → DELIVERABLE rule. Anything not
    # matched (deliver_file, Bedrock ``toolu_`` outputs, other tools)
    # falls through to DELIVERABLE. Wrapped in ``sa.text`` so the literal
    # ``%`` in the LIKE patterns reaches Postgres intact — a bare string in
    # ``op.execute`` is read through the DBAPI's pyformat (``%s``) paramstyle
    # and the percents get mistaken for bind placeholders.
    op.execute(
        sa.text(
            """
            UPDATE session_files SET origin = (CASE
              WHEN file_key LIKE '%.preview.pdf' THEN 'INTERNAL'
              WHEN file_key LIKE 'outputs/%/functions.read_image%' THEN 'INTERNAL'
              WHEN file_key LIKE 'session-files/%' THEN 'UPLOAD'
              WHEN file_key LIKE 'documents/%' THEN 'UPLOAD'
              ELSE 'DELIVERABLE'
            END)::session_file_origin
            """
        )
    )


def downgrade() -> None:
    op.drop_column("session_files", "origin")
    op.execute(f"DROP TYPE IF EXISTS {_ENUM_NAME}")
