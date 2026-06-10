"""session_files.origin: drop the DELIVERABLE default

The first origin migration (``f1a7c3e9d2b6``) gave ``origin`` a
``DEFAULT 'DELIVERABLE'`` as a backend→worker deploy-gap cushion — an
un-upgraded worker INSERTing without ``origin`` lands on a value rather
than a NOT NULL error. But that cushion mis-tags silently: such an
insert (a ``read_image`` input or preview sidecar an old worker bound)
becomes a "deliverable" and leaks into the file library.

Now that the worker stamps ``origin`` on every row, the default is
dropped so any future insert that omits it fails loudly instead. Rows
the default already mis-tagged are corrected out-of-band by
``scripts/fix_session_file_origin.py`` (data fix, not schema).

Deploy order: land this only after the origin-aware worker is fully
rolled out (no old instances) — otherwise an old worker's INSERT hits
the now-uncushioned NOT NULL constraint.

Revision ID: a3f8c1d6e4b9
Revises: f1a7c3e9d2b6
Create Date: 2026-06-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a3f8c1d6e4b9"
down_revision: Union[str, Sequence[str], None] = "f1a7c3e9d2b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("session_files", "origin", server_default=None)


def downgrade() -> None:
    op.alter_column(
        "session_files",
        "origin",
        server_default=sa.text("'DELIVERABLE'::session_file_origin"),
    )
