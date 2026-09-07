"""drop admin_audit_log

This deployment keeps no operator trail. The console is an internal tool
reached by a handful of named accounts, and the controls that matter sit
outside the application — so the table, the ``audit()`` funnel that was
the only way to commit through it, the ``AuditAction`` vocabulary and the
``/audit-logs`` endpoint all go together.

**This destroys history, and the downgrade does not bring it back.** The
column shape returns; the rows do not. Take a dump of ``admin_audit_log``
first if anything downstream reads it.

What replaces it: nothing. Every console mutation now ends with its own
``await ctx.db.commit()``. The property that made the funnel worth having
— an endpoint that forgot to audit also failed to save — is gone with it,
so a mutation that forgets to commit now silently does not stick.

The ``reason`` field goes from the request schemas too: it existed only
to be written here, and a required justification nothing records is a
form asking for work it throws away.

Follows the same removal upstream's MEP deployment made
(``c9e4b7a1f508`` there). Note master itself did **not** do this — it
kept the table and only dropped ``reason`` (``b6d2f8a04c17``), a revision
this branch must therefore never take: it would target a table that is
gone by the time it runs.

Revision ID: c8f2a5e1d94b
Revises: a3f9c1d7e2b4
Create Date: 2026-09-07

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8f2a5e1d94b"
down_revision: Union[str, Sequence[str], None] = "a3f9c1d7e2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The three indexes and the FK go with the table.
    op.drop_table("admin_audit_log")


def downgrade() -> None:
    # The shape comes back, the rows do not.
    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("staff_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["staff_id"], ["staff.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_audit_log_staff_created",
        "admin_audit_log",
        ["staff_id", "created_at"],
    )
    op.create_index(
        "ix_admin_audit_log_target",
        "admin_audit_log",
        ["target_type", "target_id"],
    )
    op.create_index(
        "ix_admin_audit_log_action_created",
        "admin_audit_log",
        ["action", "created_at"],
    )
