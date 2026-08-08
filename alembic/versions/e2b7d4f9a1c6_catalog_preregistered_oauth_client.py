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

Revision ID: e2b7d4f9a1c6
Revises: c4a91f7d20b3
Create Date: 2026-08-04

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "e2b7d4f9a1c6"
down_revision: Union[str, Sequence[str], None] = "c4a91f7d20b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _already_here() -> bool:
    """Whether this branch added these columns under its own revision.

    ``a1c7f3e9d2b6`` on ``hkgai`` is this migration written a second time —
    same table, same two columns, byte-identical ``upgrade()``, a different
    id — because the feature was developed on both branches instead of on
    master and merged down. Both are real revisions now and neither can be
    withdrawn: alembic keys on the id, so it sees two unrelated migrations
    and runs whichever it has not run.

    Guarding rather than re-pointing ``down_revision``: rewriting the chain
    is the fork ``HKGAI_MERGE.md`` argues against for the payment revisions,
    for the same reason — it has to be redone on every future sync, and it
    fails in production. This also makes the chain replayable from an empty
    database, which it is not today: there the two would collide the other
    way round.
    """
    columns = {
        c["name"] for c in inspect(op.get_bind()).get_columns("mcp_server_catalog")
    }
    return "oauth_client_id" in columns


def upgrade() -> None:
    if _already_here():
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
    """Nothing, on this branch — ``upgrade`` never added anything here.

    Not the mirror of the guard above, because the schema cannot answer the
    question this direction asks. Coming down, the columns are present either
    way: whether this revision put them there or ``a1c7f3e9d2b6`` did is not
    something ``get_columns`` can say. On ``hkgai`` it is always the latter —
    that revision is applied long before this one arrives — so dropping them
    here would take columns another revision owns, and leave *its* downgrade
    failing on columns that are gone.
    """
