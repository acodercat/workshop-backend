"""add session.tier enum (FAST / STANDARD / ADVANCED)

The session picker switched from vendor model names to capability tiers:
the user picks ``FAST`` / ``STANDARD`` / ``ADVANCED`` and the backend
resolves that to a concrete ``model_id`` (``settings.session_tier_models``)
at ``create_session``. The tier round-trips to the UI for the session
badge; ``session.model_id`` stays the server-side execution / billing key.

Existing rows predate tiers, so the column is added NOT NULL with a
temporary ``server_default`` of ``'STANDARD'`` to backfill, then the
default is dropped — matching ``session.agent_type`` / ``session.status``,
which carry no column default and are set explicitly at ``create_session``.

Revision ID: e7a2f0c5b9d4
Revises: c1d8b4f2a6e9
Create Date: 2026-05-31

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7a2f0c5b9d4"
down_revision: Union[str, Sequence[str], None] = "c1d8b4f2a6e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VALUES = ("FAST", "STANDARD", "ADVANCED")
_ENUM_NAME = "session_tier"


def upgrade() -> None:
    op.execute(
        f"CREATE TYPE {_ENUM_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _VALUES)})"
    )
    # Temporary server_default backfills existing rows to STANDARD and
    # satisfies NOT NULL during ADD COLUMN; dropped right after so every
    # INSERT sets tier explicitly (matches model_id / agent_type / status).
    op.add_column(
        "session",
        sa.Column(
            "tier",
            sa.Enum(*_VALUES, name=_ENUM_NAME, create_type=False),
            nullable=False,
            server_default=sa.text(f"'STANDARD'::{_ENUM_NAME}"),
        ),
    )
    op.alter_column("session", "tier", server_default=None)


def downgrade() -> None:
    op.drop_column("session", "tier")
    op.execute(f"DROP TYPE IF EXISTS {_ENUM_NAME}")
