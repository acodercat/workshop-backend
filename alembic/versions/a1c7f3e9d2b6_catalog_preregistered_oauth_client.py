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


revision: str = "a1c7f3e9d2b6"
down_revision: Union[str, Sequence[str], None] = "b7e2d9a4c1f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcp_server_catalog",
        sa.Column("oauth_client_id", sa.String(), nullable=True),
    )
    op.add_column(
        "mcp_server_catalog",
        sa.Column("oauth_client_secret_encrypted", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mcp_server_catalog", "oauth_client_secret_encrypted")
    op.drop_column("mcp_server_catalog", "oauth_client_id")
