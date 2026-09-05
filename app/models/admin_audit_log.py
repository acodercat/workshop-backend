"""AdminAuditLog — append-only record of every mutating action taken
through the admin console.

This platform moves real money (credit grants, payment reconciliation,
commission withdrawals). "Who changed this balance, when, and what it
was before" has to be answerable after the fact, so every write the
console performs lands a row here **in the same transaction as the
mutation it describes**. The console asks for no typed justification;
``payload``'s before → after diff is the record of what happened.
Same-transaction is the whole point: an audit trail written on a
best-effort basis is one failed commit away from recording an action that
never happened, or missing one that did.

That is not left to discipline. :func:`app.services.admin.audit` is the
only writer of this table *and* the thing that commits the change beside
it, so a console mutation without a row here does not persist.

Append-only, like ``credit_ledger`` / ``check_in`` / ``invite_redemption``
— rows are never UPDATEd or DELETEd, so the table carries only
``created_at``. Corrections are new rows, not edits.

``staff_id`` is ``RESTRICT``: an audit row is provenance, so the operator
who performed the action can't be deleted out from under it. Deactivate
staff via ``status = DISABLED`` instead of deleting the row.

The target is stored as a loose ``(target_type, target_id)`` pair with a
**string** id rather than a real FK, deliberately: actions point at rows
across many tables whose keys are variously ``int`` (user, skill) and
``UUID`` (payment_order, withdrawal_request), and an audit row must
survive its target being deleted. Referential integrity is not wanted
here — the historical fact is.

``payload`` carries the action's parameters and, for updates, the
before/after values. It is ``NOT NULL DEFAULT '{}'`` (same posture as
``session_turn.skills``) so readers never need a null-guard.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    staff_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("staff.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Dotted ``<domain>.<verb>`` identifier, e.g. ``credit.adjust``,
    # ``withdrawal.approve``, ``skill.publish``. A plain VARCHAR rather
    # than a DB enum: the set grows with every console feature, and a new
    # action should not require a migration. The controlled vocabulary
    # over it is ``app.services.admin.actions.AuditAction``, which is what
    # callers use — see that module for why it is named rather than bare
    # strings.
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    # What the action acted on. Both nullable — a few actions (a bulk
    # export, a login) have no single target.
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Action parameters + before/after diff.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    # Correlation id from ``RequestIDMiddleware`` — ties an audit row to
    # the full request trace in the logs. 64 is the middleware's own
    # acceptance cap (``observability.REQUEST_ID_MAX_LENGTH``), and the two
    # must agree: this row commits in the same transaction as the action it
    # describes, so an id the acceptor let through but this column cannot
    # hold would abort that action. A test pins the widths together; the
    # constant is not imported here so the models stay free of app-layer
    # imports.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Caller address, for after-the-fact review of where an action came
    # from. Sized for IPv6, and safe at that size because ``caller_ip``
    # stores nothing it has not parsed as an address.
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # Three read paths, three composite indexes. All are declared
        # ascending: Postgres scans a btree backwards just as cheaply, so
        # "latest first" needs no DESC index (same reasoning as
        # ``check_in``'s unique constraint doubling as its history index).
        #
        # "What did this operator do lately" — the default console view.
        Index("ix_admin_audit_log_staff_created", "staff_id", "created_at"),
        # "Everything ever done to this user / order" — the support path.
        Index("ix_admin_audit_log_target", "target_type", "target_id"),
        # "Every credit adjustment last month" — the compliance path.
        Index("ix_admin_audit_log_action_created", "action", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<AdminAuditLog(id={self.id}, staff_id={self.staff_id}, "
            f"action={self.action!r}, target={self.target_type}:{self.target_id})>"
        )
