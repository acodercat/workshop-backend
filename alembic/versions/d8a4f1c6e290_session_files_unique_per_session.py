"""session_files: one object, many sessions of its owner

The column-level UNIQUE on ``session_files.file_key`` recorded a property of
the key namespace ("OSS keys are globally unique by construction"), not an
ownership rule — but it made binding the same upload to a second session
fail with a 403, even for the user who uploaded it. This replaces it with
``UNIQUE(session_id, file_key)``: one binding per session, any number of
sessions per object.

The protection the old constraint was incidentally providing is kept, not
dropped. A share viewer legitimately holds the ``file_key`` of a file in the
session shared with them; were rebinding simply allowed, they could bind it
to a session of their own and keep minting download URLs after the share
was revoked. ``_bind_session_files`` now refuses a key held by another
*user* (``assert_files_owned_by``) instead of one bound to another session.

``ix_session_files_file_key`` is not optional. That ownership check looks a
batch of keys up with no session filter, and the read used to ride the
UNIQUE index being dropped here.

That name is free despite having been used before: ``b1c7e29f4d8a`` turned
the original unique index of that name into a constraint with
``ADD CONSTRAINT ... USING INDEX``, which *renames* the btree to
``session_files_file_key_key``. So the index created below is a new,
non-unique one that happens to reclaim the natural name.

**Ordering across repositories.** ``core/agent-worker`` records tool outputs
with ``ON CONFLICT ... DO NOTHING``. While it named ``(file_key)`` as the
conflict target, dropping the constraint here would make Postgres reject
every such INSERT ("no unique or exclusion constraint matching the ON
CONFLICT specification"). The worker has since moved to the target-free
form (agent-worker ``76d919a``), which is equivalent under either schema;
this must not be applied until that worker is deployed.

Revision ID: d8a4f1c6e290
Revises: c5e1a8f3b92d
Create Date: 2026-09-14

"""

from typing import Sequence, Union

from alembic import op

revision: str = "d8a4f1c6e290"
down_revision: Union[str, Sequence[str], None] = "c5e1a8f3b92d"
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


def downgrade() -> None:
    # Restoring the old constraint fails if any object is bound to more than
    # one session by then — which is exactly what this release makes
    # possible. Deliberately left to fail loudly rather than deleting rows to
    # make room: which binding to drop is not a migration's decision.
    op.create_unique_constraint(_OLD_FILE_KEY_UNIQUE, "session_files", ["file_key"])
    op.drop_index("ix_session_files_file_key", table_name="session_files")
    op.drop_constraint(
        "uq_session_files_session_id_file_key", "session_files", type_="unique"
    )
