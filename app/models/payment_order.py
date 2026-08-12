"""Buy-side payment orders — one row per recharge attempt.

A ``payment_order`` is the durable record of a user's RMB top-up
through a payment provider (汇付斗拱 / Huifu today). It is the bridge
between an external payment and the internal credit ledger:

    user picks ¥amount  →  payment_order (PENDING, credits frozen)
                        →  provider QR  →  user pays
                        →  async notify / reconcile  →  PAID
                        →  credits.apply_topup  →  TOPUP ledger row

**The order never moves credits itself.** Balance changes go through
``credits.apply_topup`` exactly once (idempotent on
``"hf:"+out_trade_no``); ``ledger_id`` back-links the resulting TOPUP
row so an auditor can trace a payment to the credits it granted. This
mirrors the ``credit_reservation`` → ``credit_ledger`` back-link.

Two amounts are frozen at creation and never recomputed:

- ``amount_fen`` — what the customer pays, in RMB cents (integer, no
  float; ``trans_amt`` on the provider wire is a 2-decimal yuan
  string, converted only at the ``huifu.py`` boundary).
- ``credit_micro`` — the µ-credits to grant, snapshotted via
  ``credits_from_cny`` at create time. Freezing it means a
  ``credit_price_cny`` change between "scan" and "pay" can't alter
  what the user receives — the same customer-trust property as the
  frozen ``rate_snapshot`` on a reservation.

``out_trade_no`` is our request id sent to the provider (= 汇付
``req_seq_id``); ``provider_txn_id`` is the provider's own id (= 汇付
``hf_seq_id``), filled on success. ``reference`` (JSONB) carries
provider-specific debug context (``trade_type``, ``req_date``,
``qr_code``, ``hf_seq_id``, notify excerpt) — different providers
correlate to different fields, so a discriminated column-per-field
would be a sparse table.

The async notify is at-least-once (汇付 retries 3× on timeout), so
``handle_webhook`` is idempotent: the row lock + the ``status``
guard (``PENDING`` → ``PAID`` once) + ``apply_topup``'s
``idempotency_key`` together make a duplicate delivery a no-op.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class PaymentProvider(StrEnum):
    """Which payment provider handled the order.

    - ``HUIFU`` — 汇付斗拱 (Huifu Dougong), the only provider today.

    A new provider is a new adapter behind the ``PaymentGateway`` protocol
    plus a member here. Member values match the Postgres
    ``payment_provider`` enum and are UPPERCASE, matching the
    ``credit_ledger_kind`` / ``session_status`` convention.
    """

    HUIFU = "HUIFU"


class PaymentOrderStatus(StrEnum):
    """Lifecycle status of a payment order.

    - ``PENDING`` — created, QR issued, awaiting payment. Default for a
      fresh row. The only state the reconciler / webhook may transition
      out of.
    - ``PAID`` — provider confirmed payment; ``apply_topup`` granted the
      credits and ``ledger_id`` back-links the TOPUP row. Terminal.
    - ``EXPIRED`` — TTL elapsed with no payment (set lazily on read or by
      the reconciler). Terminal.
    - ``FAILED`` — provider reported a failed / amount-mismatched
      transaction. Terminal; no credits granted.

    Only ``PENDING`` is non-terminal. Member values match the Postgres
    ``payment_order_status`` enum; UPPERCASE per project convention.
    """

    PENDING = "PENDING"
    PAID = "PAID"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class PaymentOrder(Base):
    __tablename__ = "payment_order"

    # UUID PK so the id is opaque in ``GET /payments/orders/{id}`` (polled
    # by the browser) and can't be enumerated across users — same rationale
    # as ``session.id``. Distinct from ``out_trade_no``, the provider-facing
    # business key.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # ``RESTRICT`` rather than ``CASCADE`` — a paid order is financial
    # history; deleting a user with payment rows should fail loudly rather
    # than silently drop the audit trail (mirrors ``credit_ledger.user_id``).
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        SAEnum(
            PaymentProvider,
            name="payment_provider",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    # Our request id sent to the provider (= 汇付 ``req_seq_id``). UNIQUE so
    # a retried create can't double-book and the webhook can look the order
    # up by the id the provider echoes back.
    out_trade_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # The provider's own transaction id (= 汇付 ``hf_seq_id``), filled when
    # the payment is confirmed. NULL while PENDING.
    provider_txn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # What the customer pays, in RMB cents (整数分). Integer money — no float;
    # the 2-decimal yuan string on the provider wire is converted only at the
    # ``huifu.py`` boundary, and the webhook amount-tamper check compares fen.
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # µ-credits to grant, frozen at create via ``credits_from_cny`` so a
    # ``credit_price_cny`` change mid-flight can't re-price an open order.
    credit_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[PaymentOrderStatus] = mapped_column(
        SAEnum(
            PaymentOrderStatus,
            name="payment_order_status",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        server_default=text("'PENDING'"),
    )
    # Back-link to the TOPUP ledger row that granted the credits, set
    # atomically with the PENDING→PAID flip. ``RESTRICT`` because the ledger
    # is append-only (a row is never deleted, so this never dangles).
    ledger_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("credit_ledger.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Provider-specific debug/audit context — ``trade_type``, ``req_date``,
    # ``qr_code``, ``hf_seq_id``, async-notify excerpt. JSONB because the
    # fields vary by provider.
    reference: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    # When the QR stops being payable. Set to ``now + payment_order_ttl_seconds``
    # at create (and passed to the provider as ``time_expire``); the reconciler
    # and the lazy read-path expire PENDING orders past this.
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # Backs ``GET /payments/orders`` history listing (newest first).
        # Postgres doesn't auto-index FK columns; without this a per-user
        # list would seq-scan the whole table.
        Index(
            "ix_payment_order_user_created",
            "user_id",
            text("created_at DESC"),
        ),
        # The reconciler scans only actionable PENDING orders by expiry.
        # Partial so it stays tiny forever even as paid/expired history
        # grows — mirrors ``credit_reservation``'s OPEN partial index.
        Index(
            "ix_payment_order_pending",
            "expires_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<PaymentOrder(id={self.id}, user_id={self.user_id}, "
            f"out_trade_no={self.out_trade_no!r}, status={self.status.value!r})>"
        )
