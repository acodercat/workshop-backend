"""mcp_server_catalog: a pre-registered OAuth app per entry

Some providers offer neither dynamic client registration nor CIMD — Figma
requires an app created by hand in its developer console. Until now such a
server could only be added as a per-user custom Connector with the credentials
typed into the form; a catalog entry for it would fail at authorize time, when
the add flow finds no way to mint a client.

Two nullable columns, so every existing entry keeps its behaviour:

- ``oauth_client_id`` — the app's public identifier, plaintext.
- ``oauth_client_secret_encrypted`` — Fernet ciphertext under
  ``mcp_credential_encryption_key``, the same envelope every stored Connector
  credential already uses. Never plaintext, and never exposed by the API
  (``ConnectorCatalogEntryResponse`` lists its fields explicitly).

Set and rotated via ``scripts/manage_connector_catalog.py set-oauth-client``,
which reads the secret from the environment or a prompt — not argv.

Revision ID: a1c7f3e9d2b6
Revises: b7e2d9a4c1f5
Create Date: 2026-08-04

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "a1c7f3e9d2b6"
down_revision: Union[str, Sequence[str], None] = "b7e2d9a4c1f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns_present() -> bool:
    """Whether the pair is already on the table.

    This revision and ``e2b7d4f9a1c6`` add the same two columns: the feature was
    written once on ``master`` and once on ``hkgai`` instead of being merged
    down, so alembic holds two ids for one change and neither is an ancestor
    of the other. Whichever runs first adds them; the other must find them
    there and do nothing.

    Order is not fixed and cannot be assumed. On the deployed database
    ``a1c7f3e9d2b6`` was applied months before ``e2b7d4f9a1c6`` arrived, but
    from an empty one alembic's own topological order puts ``e2b7d4f9a1c6``
    first — so the guard has to sit on both, phrased the same way, and read
    the schema rather than the calendar.

    ``downgrade`` asks the same question for the same reason: coming back down
    the first to run drops them and the second finds nothing.
    """
    columns = {
        column["name"] for column in inspect(op.get_bind()).get_columns("mcp_server_catalog")
    }
    return "oauth_client_id" in columns


def upgrade() -> None:
    if _columns_present():
        return
    op.add_column(
        "mcp_server_catalog",
        sa.Column("oauth_client_id", sa.String(), nullable=True),
    )
    op.add_column(
        "mcp_server_catalog",
        sa.Column("oauth_client_secret_encrypted", sa.String(), nullable=True),
    )


def downgrade() -> None:
    if not _columns_present():
        return
    op.drop_column("mcp_server_catalog", "oauth_client_secret_encrypted")
    op.drop_column("mcp_server_catalog", "oauth_client_id")
