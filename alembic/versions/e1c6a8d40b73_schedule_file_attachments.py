"""session_files: one object, many sessions — and schedule attachments

A scheduled task creates a fresh session every firing, so re-attaching the
same upload meant binding one ``file_key`` more than once. The column-level
UNIQUE on ``session_files.file_key`` made the second firing fail with a 403
and no way forward.

That constraint recorded a property of the key namespace ("OSS keys are
globally unique by construction"), not an ownership rule. Ownership was the
explicit cross-session rejection in ``_bind_session_files`` — which this
release replaces with a cross-*user* one, so the protection it was
incidentally providing (a share viewer legitimately holds a ``file_key``;
binding it to their own session would outlive the share) is kept rather
than dropped. See that function for the replacement.

``ix_session_files_file_key`` is not optional. ``_bind_session_files`` looks
a batch of keys up with no session filter to find who already owns them, and
that read used to ride the UNIQUE index being dropped here.

That name is free despite having been used before: ``b1c7e29f4d8a`` turned
the original unique index of that name into a constraint with
``ADD CONSTRAINT ... USING INDEX``, which *renames* the btree to
``session_files_file_key_key``. So the index created below is a new,
non-unique one that happens to reclaim the natural name.

**Ordering across repositories.** ``core/agent-worker`` records tool outputs
with ``ON CONFLICT ... DO NOTHING`` and is shared by this deployment and
hkgai's. While it named ``(file_key)`` as the conflict target, dropping the
constraint here would make Postgres reject every such INSERT ("no unique or
exclusion constraint matching the ON CONFLICT specification"). The worker
was changed to the target-free form first — equivalent under either schema,
so hkgai needs no migration at all — and this must not be applied until that
worker is deployed.

Revision ID: e1c6a8d40b73
Revises: b5e9d3f70a26
Create Date: 2026-09-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1c6a8d40b73"
down_revision: Union[str, Sequence[str], None] = "b5e9d3f70a26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Postgres names a column-level UNIQUE ``{table}_{column}_key``.
_OLD_FILE_KEY_UNIQUE = "session_files_file_key_key"


def upgrade() -> None:
    # Widen first, narrow second: between these two statements the table is
    # over-constrained rather than under-constrained, so a concurrent insert
    # can only be rejected, never duplicated.
    op.create_unique_constraint(
        "uq_session_files_session_id_file_key",
        "session_files",
        ["session_id", "file_key"],
    )
    op.create_index("ix_session_files_file_key", "session_files", ["file_key"])
    op.drop_constraint(_OLD_FILE_KEY_UNIQUE, "session_files", type_="unique")

    op.create_table(
        "schedule_file",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("schedule_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("file_key", sa.String(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["schedule_id"], ["schedule.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schedule_id", "file_key", name="uq_schedule_file_schedule_id_file_key"
        ),
    )
    op.create_index("ix_schedule_file_user_id", "schedule_file", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_schedule_file_user_id", table_name="schedule_file")
    op.drop_table("schedule_file")

    # Restoring the old constraint fails if any object is bound to more than
    # one session by then — which is exactly what this release makes
    # possible. Deliberately left to fail loudly rather than deleting rows to
    # make room: which binding to drop is not a migration's decision.
    op.create_unique_constraint(_OLD_FILE_KEY_UNIQUE, "session_files", ["file_key"])
    op.drop_index("ix_session_files_file_key", table_name="session_files")
    op.drop_constraint(
        "uq_session_files_session_id_file_key", "session_files", type_="unique"
    )
