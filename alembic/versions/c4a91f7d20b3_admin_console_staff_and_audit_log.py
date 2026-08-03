"""admin console — staff accounts and the audit trail

Two tables behind the internal operations console (``app.admin_main``).

``staff`` is deliberately **not** a flag on ``user``. Operator identity is
not customer identity: running the console must not require holding a
customer account with its credit balance and sessions, and a customer row
must never be one UPDATE away from platform-wide privileges. Credentials
and revocation are independent too, so a staff force-logout can't disturb
a customer session. The role ladder here is *platform*-scoped and has
nothing to do with ``organization_member.role``, which is tenant-scoped.

``admin_audit_log`` is append-only, like ``credit_ledger`` and
``check_in`` — rows are never UPDATEd or DELETEd, so it carries only
``created_at``. Every console mutation writes one in the same transaction
as the change it describes; that atomicity is what makes the trail worth
having, and it is enforced in ``app.services.admin.audit``.

``staff_id`` is ``RESTRICT``: an audit row is provenance, so the operator
who acted can't be deleted out from under it. Deactivate departing staff
with ``status = DISABLED`` instead. The target is a loose
``(target_type, target_id)`` pair with a **string** id and no FK, because
actions point at rows across tables keyed variously by ``int`` and
``UUID``, and an audit row must outlive its target.

Three composite indexes for the three questions the log gets asked: what
did this operator do lately (the console's default view), everything ever
done to this row (support), every action of a kind in a window
(compliance). All ascending — Postgres scans a btree backwards just as
cheaply, so "latest first" needs no DESC index.

**Bootstrap:** this creates no accounts. Provision the first ``OWNER``
with ``scripts/manage_staff.py`` after upgrading; until then the console
has no way in, which is the correct default for a table that grants
platform-wide privileges.

Revision ID: c4a91f7d20b3
Revises: d3b7f5a1c682
Create Date: 2026-08-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "c4a91f7d20b3"
down_revision: Union[str, Sequence[str], None] = "d3b7f5a1c682"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    staff_role = postgresql.ENUM(
        "VIEWER", "OPERATOR", "OWNER", name="staff_role", create_type=False
    )
    staff_status = postgresql.ENUM(
        "ACTIVE", "DISABLED", name="staff_status", create_type=False
    )
    staff_role.create(op.get_bind(), checkfirst=True)
    staff_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "staff",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        # Operator-chosen, not auto-generated like ``user.username``:
        # these accounts are provisioned by hand.
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        # Least privilege by default: a row inserted without an explicit
        # role can read the console but cannot change anything.
        sa.Column(
            "role", staff_role, nullable=False, server_default="VIEWER",
        ),
        sa.Column(
            "status", staff_status, nullable=False, server_default="ACTIVE",
        ),
        # Bumped to invalidate every token issued so far. Mirrors
        # ``user.token_version``; the staff decoder compares against it.
        sa.Column(
            "token_version", sa.Integer(), nullable=False, server_default="0",
        ),
        # Nullable: an account provisioned but never used has no sensible
        # zero value, and NULL reads as "never".
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        # The unique constraint's backing index also serves the login
        # lookup — no separate index on username.
        sa.UniqueConstraint("username", name="uq_staff_username"),
    )

    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("staff_id", sa.Integer(), nullable=False),
        # Dotted ``<domain>.<verb>``. A plain string rather than an enum:
        # the vocabulary grows with every console feature, and a new
        # action should not require a migration. The controlled list is
        # ``app.services.admin.actions.AuditAction``.
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        # NOT NULL DEFAULT '{}' (same posture as ``session_turn.skills``)
        # so readers never need a null-guard.
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Correlation id from ``RequestIDMiddleware`` — ties a row to the
        # full request trace in the logs.
        sa.Column("request_id", sa.String(length=64), nullable=True),
        # Sized for IPv6.
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["staff_id"],
            ["staff.id"],
            name="fk_admin_audit_log_staff_id",
            ondelete="RESTRICT",
        ),
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


def downgrade() -> None:
    op.drop_index("ix_admin_audit_log_action_created", table_name="admin_audit_log")
    op.drop_index("ix_admin_audit_log_target", table_name="admin_audit_log")
    op.drop_index("ix_admin_audit_log_staff_created", table_name="admin_audit_log")
    op.drop_table("admin_audit_log")

    op.drop_table("staff")

    # Dropped after their last consumer, and only then — an enum type
    # outliving its column would block a re-upgrade with "type already
    # exists".
    sa.Enum(name="staff_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="staff_role").drop(op.get_bind(), checkfirst=True)
