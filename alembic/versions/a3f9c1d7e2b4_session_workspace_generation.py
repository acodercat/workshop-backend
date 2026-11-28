"""workspace generations: session.workspace_* columns + sandbox.workspace_generation

The sandbox reaper's workspace-reclaim tier deletes a session's ``/workspace``
storage (a k8s PVC or a docker host directory) once every sandbox for it is
``TERMINATED`` and it has sat idle past the GC window. Until now it selected
candidates, released the row lock, then deleted — and in that window a
session could wake up, mount the very storage on the list, and have its new
files deleted out from under it. No lease closes that: the deletion is
unbounded in time, runs in a thread the reaper cannot interrupt, and can
fail and need retrying.

So the reaper does not exclude the session from its storage. It moves the
session to a new **workspace generation** in the same statement that selects
it, and every sandbox created from then on mounts storage named for that
generation; the old storage is unreachable to any turn, and deleting it can
take as long as it takes. Four columns, all written only by the worker
(``agent_worker/infra/sandbox_repository.py``) and read only by the worker's
``claim_next_turn`` and its reaper; the backend declares them and touches
none:

- ``session.workspace_generation`` — the generation a fresh container
  mounts. Generation 0 keeps the pre-generation storage names, so existing
  rows need no backfill and existing storage is found where it already is.
- ``session.workspace_reclaimed_generation`` — the generation up to which
  (exclusive) the older storage is confirmed gone. Behind
  ``workspace_generation`` means a deletion is still pending and is retried
  before any further bump.
- ``session.workspace_reclaim_attempted_at`` — when the reaper last tried;
  retries are ordered least-recently-attempted first so a permanently
  undeletable workspace cannot starve the rest.
- ``sandbox.workspace_generation`` — the generation that container
  mounted. A turn reconnects only to a container on the session's current
  generation, so a container recorded by a worker whose lease had already
  expired — on storage the reaper has since moved the session past — is
  never handed out again.

No index: the reclaim scan drives from ``session`` with a ``LATERAL`` over
``sandbox`` and is bounded by ``LIMIT``; the sandbox lookup on claim already
has ``ix_sandbox_session_id_live``-shaped support and adds one equality.

**hkgai note — the parent differs from upstream on purpose.** Upstream this
revision hangs off ``f4c8a2b91d63``, which **does not exist on this branch
at all** (it is one of six trunk revisions this fork never took). Copied
verbatim, ``alembic upgrade head`` fails with "Can't locate revision
identified by 'f4c8a2b91d63'". It is re-pointed at this branch's head
instead. The revision **id** is deliberately kept identical to upstream's
so the two chains stay comparable — do not "correct" ``down_revision``
back to ``f4c8a2b91d63`` on a later sync.

Bringing the intervening trunk chain over instead would *look* tidier —
it is linear and rooted exactly at this branch's head — but
``b6d2f8a04c17`` in the middle of it drops ``admin_audit_log.reason``,
a column this branch declared and read at the time.

Revision ID: a3f9c1d7e2b4
Revises: a1f4c7e02b31
Create Date: 2026-09-07

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3f9c1d7e2b4"
down_revision: Union[str, Sequence[str], None] = "a1f4c7e02b31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session",
        sa.Column(
            "workspace_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "session",
        sa.Column(
            "workspace_reclaimed_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "session",
        sa.Column("workspace_reclaim_attempted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "sandbox",
        sa.Column(
            "workspace_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("sandbox", "workspace_generation")
    op.drop_column("session", "workspace_reclaim_attempted_at")
    op.drop_column("session", "workspace_reclaimed_generation")
    op.drop_column("session", "workspace_generation")
