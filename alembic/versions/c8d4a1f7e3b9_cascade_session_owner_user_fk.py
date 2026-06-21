"""cascade session-owner user_id FKs

Standardizes the session-ownership family's owner FK to ``ON DELETE
CASCADE``. ``session.user_id`` and ``session_site.user_id`` were created
with the implicit default (``NO ACTION``) — the lone outliers among the
owned-content user FKs, which are all ``CASCADE`` (``skill``,
``message_feedback``, ``organization_member``, ``user_skill_preference``;
``session_files`` is born CASCADE in the prior migration). Money / audit
FKs stay ``RESTRICT`` and ``user_feedback`` stays ``SET NULL`` — those are
deliberate and unchanged.

After this, deleting a ``user`` cascades their sessions → and via the
session cascade, their messages / files / sites. This is the no-orphan-row
backstop; real account erasure stays an app-level routine (it must also
delete the OSS objects behind files / sites, which a DB cascade can't).

DDL only — each constraint is dropped and re-added with the new action;
**no rows are read or modified**, and the re-add re-validates against
data that already satisfies the FK.

Revision ID: c8d4a1f7e3b9
Revises: b7e3f29a1c4d
Create Date: 2026-06-19

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c8d4a1f7e3b9'
down_revision: Union[str, Sequence[str], None] = 'b7e3f29a1c4d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, FK constraint name) — owner FKs flipped NO ACTION → CASCADE.
_OWNER_FKS = (
    ('session', 'session_user_id_fkey'),
    ('session_site', 'session_site_user_id_fkey'),
)


def upgrade() -> None:
    """Upgrade schema."""
    for table, constraint in _OWNER_FKS:
        op.drop_constraint(constraint, table, type_='foreignkey')
        op.create_foreign_key(
            constraint, table, 'user', ['user_id'], ['id'], ondelete='CASCADE'
        )


def downgrade() -> None:
    """Downgrade schema."""
    # Restore the prior implicit NO ACTION (no explicit ondelete).
    for table, constraint in _OWNER_FKS:
        op.drop_constraint(constraint, table, type_='foreignkey')
        op.create_foreign_key(
            constraint, table, 'user', ['user_id'], ['id']
        )
