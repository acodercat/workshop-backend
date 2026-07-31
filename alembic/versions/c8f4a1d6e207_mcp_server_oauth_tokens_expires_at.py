"""mcp_server.oauth_tokens_expires_at

When the stored OAuth access token stops being valid.

``OAuthToken.expires_in`` is seconds *from issuance*, and issuance was never
recorded — so nothing in ``oauth_tokens_encrypted`` can answer "is this token
still good?" after the process that fetched it is gone.

This is the column that says whether a token still needs renewing. Both
services read it: the backend's token refresher selects on it, and the worker
reads it per turn to decide whether to renew a Connector's grant before the
handshake — and writes it after one. A grant with no deadline is one nothing
ever renews, so its access token simply dies and the user is sent back to the
browser.

Absolute, not a duration, so it survives being read by a different process at a
different time — which is the whole point.

NULL means "no known expiry", and only rows authorized before this column
existed carry it. So, deliberately, does a grant nothing can renew: a server
that issues neither ``expires_in`` nor a refresh token gets NULL rather than an
assumed lifetime, because assuming one would have the refresher pick the row up
an hour later, find nothing to rotate with, and ask the user to re-authorize a
token that had not expired. A grant that *can* be renewed and states no
lifetime gets the assumption, since acting early costs one request.

**Deploy note: apply before deploying either service.** The backend's token
refresher selects on this column and the worker reads and writes it on every
OAuth Connector it connects; on an old schema the refresher renews nothing and
every such connect attempt raises.

Revision ID: c8f4a1d6e207
Revises: b5c9e2f7a184
Create Date: 2026-07-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c8f4a1d6e207"
down_revision: Union[str, Sequence[str], None] = "b5c9e2f7a184"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcp_server",
        sa.Column(
            "oauth_tokens_expires_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("mcp_server", "oauth_tokens_expires_at")
