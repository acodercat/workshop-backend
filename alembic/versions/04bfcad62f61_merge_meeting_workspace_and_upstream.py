"""merge the meeting workspace and upstream heads

The federated identity and Meeting workspace revisions grew from
``c7d2e9a4b1f3`` alongside upstream's message queue, pinning and schedule
revisions, and a database already carries the hkgai pair: joining the two
lines leaves every applied revision where it is. They touch separate tables,
so the order they run in does not matter and there is nothing to do here.

Revision ID: 04bfcad62f61
Revises: d5a1f3c7e9b2, e09400a96810
Create Date: 2026-11-27

"""

from typing import Sequence, Union

revision: str = "04bfcad62f61"
down_revision: Union[str, Sequence[str], None] = ("d5a1f3c7e9b2", "e09400a96810")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
