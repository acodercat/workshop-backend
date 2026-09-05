"""index credit_reservation.session_id

``analytics.session_spend`` asks "what did these sessions cost" for one
page of the console's session list: filter ``credit_reservation`` by a
handful of ``session_id``s, then join the ledger through
``ix_credit_ledger_reservation_id``.

The existing ``uq_credit_reservation_open_session`` cannot serve that.
It is partial on ``state = 'OPEN'``, and spend is read off *settled*
reservations — the rows that index deliberately excludes. Without this
one the filter has no access path and Postgres falls back to a
sequential scan of a table that grows by one row per turn, forever, on
every page load of a support screen.

Plain rather than partial: reservations with a NULL ``session_id``
(there are none today, but the column is nullable) simply do not appear
in the index, and every row that carries a session is in scope.

Revision ID: d5b1e93f7a48
Revises: c2f8a41b7e60
Create Date: 2026-09-04

"""

from typing import Sequence, Union

from alembic import op

revision: str = "d5b1e93f7a48"
down_revision: Union[str, Sequence[str], None] = "c2f8a41b7e60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_credit_reservation_session_id",
        "credit_reservation",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_credit_reservation_session_id", table_name="credit_reservation")
