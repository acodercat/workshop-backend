"""session_files: add filename / mime_type / size_bytes

Denormalizes the file's presentation metadata onto ``session_files`` so
the file library (``GET /files``) can render name / type / size without
scanning the message blocks. All three are **nullable** — the producers
(backend upload path, worker tool-output path) populate them going
forward; history is filled by ``scripts/backfill_session_file_metadata.py``.

Nullable on purpose: unlike the ``origin`` column, there is no deploy-order
constraint here. An un-upgraded producer that omits these simply writes
null, and the reader falls back to deriving a name from the key — no NOT
NULL violation, so the migration is safe to land before either service
is redeployed.

Revision ID: b4e2d8f1a9c3
Revises: a3f8c1d6e4b9
Create Date: 2026-06-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b4e2d8f1a9c3"
down_revision: Union[str, Sequence[str], None] = "a3f8c1d6e4b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session_files", sa.Column("filename", sa.String(), nullable=True)
    )
    op.add_column(
        "session_files", sa.Column("mime_type", sa.String(), nullable=True)
    )
    op.add_column(
        "session_files", sa.Column("size_bytes", sa.BigInteger(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("session_files", "size_bytes")
    op.drop_column("session_files", "mime_type")
    op.drop_column("session_files", "filename")
