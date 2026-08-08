"""mcp_server — one Connector per server, and names free to repeat

``uq_mcp_server_user_id_name`` said a user could not name two Connectors the
same thing, and said nothing about pointing two at one server. That is backwards
on both halves: the URL is what a Connector *is* — two rows for one server are
the same tools arriving twice in a turn's catalog, and two credentials to keep
in step — while the name is a label the user chose, and two of them colliding
harms nothing.

It also unblocks a localized catalog. An official Connector's name is copied
from ``mcp_server_catalog`` at add time; once that name has translations, a
name-based constraint would let *which language the user was reading* decide
whether an unrelated second server could be added at all.

Duplicates have to go first, and this deletes them rather than stopping: the
rows are a handful of test entries on a single account, confirmed by hand
before this was written, and the deployment is one operator's own. The rule is
deterministic — keep the row most likely to still work (connected first, then
one holding OAuth tokens, then the oldest id) — so a re-run reaches the same
state. Note the delete cascades to ``session_mcp_server``: a session that had a
losing row switched on loses it, which is why the keeper is the usable one.

If that trade is ever wrong for an installation, stop here, prune by hand, and
re-run — everything below the delete is idempotent.

Revision ID: a3d7f61b8e04
Revises: b8f2a91c47d3
Create Date: 2026-08-08
"""
from typing import Sequence, Union

from alembic import op


revision: str = "a3d7f61b8e04"
down_revision: Union[str, Sequence[str], None] = "b8f2a91c47d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM mcp_server AS m
        USING (
            SELECT id
            FROM (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY user_id, url
                        ORDER BY
                            connected DESC,
                            (oauth_tokens_encrypted IS NOT NULL) DESC,
                            id
                    ) AS rank
                FROM mcp_server
            ) ranked
            WHERE rank > 1
        ) AS loser
        WHERE m.id = loser.id
        """
    )
    op.drop_constraint(
        "uq_mcp_server_user_id_name", "mcp_server", type_="unique"
    )
    op.create_unique_constraint(
        "uq_mcp_server_user_id_url", "mcp_server", ["user_id", "url"]
    )


def downgrade() -> None:
    # The name constraint cannot simply be put back: nothing stopped duplicate
    # names while it was gone, so re-adding it blindly would fail on data this
    # migration made legal. Suffix the repeats — losing the exact label the
    # user typed, which is the honest cost of going back.
    op.execute(
        """
        UPDATE mcp_server AS m
        SET name = left(m.name, 56) || ' (' || m.id || ')'
        FROM (
            SELECT
                id,
                row_number() OVER (PARTITION BY user_id, name ORDER BY id) AS rank
            FROM mcp_server
        ) ranked
        WHERE m.id = ranked.id AND ranked.rank > 1
        """
    )
    op.drop_constraint(
        "uq_mcp_server_user_id_url", "mcp_server", type_="unique"
    )
    op.create_unique_constraint(
        "uq_mcp_server_user_id_name", "mcp_server", ["user_id", "name"]
    )
