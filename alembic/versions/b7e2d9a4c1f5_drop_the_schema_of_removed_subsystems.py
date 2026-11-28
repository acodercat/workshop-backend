"""Drop the schema of the subsystems this deployment removed — hkgai only

Two of them: the money subsystem (`payment_order`, `commission_ledger`,
`withdrawal_request`, `user.commission_balance_fen`) and the phone channel
(`user.phone`). The code went in `5fcf505` and its neighbours — services,
endpoints, schemas, models. The schema stayed, because until now this branch
took that from master's chain verbatim and owned no revision of its own.

What that cost: `Base.metadata` disagreed with the database it is compared
against, which is what made `revision --autogenerate` here write `drop_table`
for master's live tables. `user.phone` was the milder half of the same thing —
the column was declared but nothing read it, and its partial unique index
disagreed with the `unique=True` the model asked for, so autogenerate was never
silent either.

**This revision is the point where that stops.** It is the first migration this
branch owns, and owning one has consequences the next person should know before
adding a second:

- Master's chain still applies here verbatim, but its head and this one are
  siblings. After every sync that brings new master revisions, `alembic heads`
  reports two and the fix is one `alembic merge heads` — once per sync, not once
  per revision, because master's new revisions form a chain and only the first
  branches.
- A future master revision that *touches* any of this will fail here, loudly, at
  `upgrade` — about one in ten of them historically (`e1c5a9d7b3f4` is the shape
  to expect: a general housekeeping revision that alters
  `withdrawal_request.ledger_id` in passing). The fix at that moment is a local
  revision on this branch, not an edit to master's file.

**hkgai note:** how this branch's chain relates to master's now — revisions
not taken, re-pointed or edited in place — is kept in ``HKGAI_MERGE.md``
("⚠️ Alembic migration chain"); read that rather than the first point above.

Revision ID: b7e2d9a4c1f5
Revises: d3b7f5a1c682
Create Date: 2026-08-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b7e2d9a4c1f5"
down_revision: Union[str, Sequence[str], None] = "d3b7f5a1c682"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Dependency order: `withdrawal_request.ledger_id` → `commission_ledger.id`, so
# the referencing table goes first. `payment_order` points outward only
# (`credit_ledger`, `user`), both of which stay.
_TABLES = ("withdrawal_request", "commission_ledger", "payment_order")

# Types these tables own outright — no table that stays uses them, so they leave
# with the tables. Dropping a table does not drop its enum type in Postgres.
_ENUMS = (
    "withdrawal_status",
    "commission_ledger_kind",
    "payment_order_status",
    "payment_provider",
)


def upgrade() -> None:
    connection = op.get_bind()

    # Everything is checked before anything is dropped. One pass would drop the
    # first table and *then* discover a row in the second, leaving the operator
    # to decide what to do next with the evidence already half gone. The rows are
    # the whole point: nothing on this deployment writes any of this, so one
    # existing means an assumption here is wrong and the drop should wait for a
    # human rather than proceed.
    for table in _TABLES:
        rows = connection.execute(
            sa.select(sa.func.count()).select_from(sa.table(table))
        ).scalar_one()
        if rows:
            raise RuntimeError(
                f"{table} holds {rows} row(s). This deployment is not supposed "
                f"to have written it, so review before dropping — the rows are "
                f"the only evidence of whatever did."
            )
    numbered = connection.execute(
        sa.text('SELECT count(*) FROM "user" WHERE phone IS NOT NULL')
    ).scalar_one()
    if numbered:
        raise RuntimeError(
            f"{numbered} user(s) have a phone number. The phone channel is gone "
            f"from this deployment, so somebody signed up through one that no "
            f"longer exists — review before dropping the column they are in."
        )

    for table in _TABLES:
        op.drop_table(table)
    for enum in _ENUMS:
        # No `IF EXISTS`: each of these is created by the same revision that
        # creates its table, so one missing means something is wrong about this
        # database that the operator should hear about rather than step over.
        op.execute(f"DROP TYPE {enum}")
    op.drop_column("user", "commission_balance_fen")
    # Takes `ix_user_phone` (the partial unique index from `e5c1a9b7d2f4`) with
    # it — Postgres drops an index when its last column goes.
    op.drop_column("user", "phone")


def downgrade() -> None:
    # Deliberately not reversible. Recreating the tables means copying four
    # revisions' worth of columns, constraints and indexes out of master's chain
    # into this branch, where they would then be a second, drifting definition of
    # tables this deployment does not have. To go back, take master's schema.
    raise NotImplementedError(
        "Irreversible: restore the removed subsystems from master's chain instead."
    )
