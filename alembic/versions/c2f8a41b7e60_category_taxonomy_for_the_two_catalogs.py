"""skill_category / connector_category — a browsing taxonomy for the two catalogs

Two tables, not one with a scope column. The vocabularies do not overlap — a
skill is a capability, a Connector is an outside system — and a shared table
would need every query to remember a scope filter, where forgetting is silent
and shows up as another catalog's tabs in the operator's picker. Two tables put
that guarantee in the foreign key, and give each page a ``sort_order`` that
starts at 0.

Written out twice rather than looped over a pair of table names, so every
constraint and index name here is a literal that matches the one in the model's
``__table_args__`` — those names are what someone greps when a constraint fires.

Both are shaped like ``mcp_server_catalog``, which they sit beside: a canonical
``name`` plus a nullable ``{locale: name}`` map, ``published`` for whether the
operator offers it, ``sort_order`` for where it lands in the strip.

``ON DELETE SET NULL`` on both foreign keys, the same choice as
``mcp_server.catalog_id``: retiring a category must not take the entries in it
with it. They fall back to reading as uncategorised, which is the state every
row starts in anyway.

No index on either ``category_id``. Both parent tables are curated shortlists
an operator maintains by hand — Postgres will sequentially scan them whatever
is on the column, so an index here would be maintenance without a reader.

Not ``CONCURRENTLY``: the new tables are empty, and adding a nullable column
with no default only touches the system catalog. The repo reserves
``CONCURRENTLY`` for indexing a populated hot table.

Revision ID: c2f8a41b7e60
Revises: b6d2f8a04c17
Create Date: 2026-09-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2f8a41b7e60"
down_revision: Union[str, Sequence[str], None] = "b6d2f8a04c17"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "skill_category",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("name_i18n", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_skill_category_name"),
    )
    # The strip is only ever read published-first in display order.
    op.create_index(
        "ix_skill_category_published_sort_order",
        "skill_category",
        ["published", "sort_order"],
    )

    op.create_table(
        "connector_category",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("name_i18n", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_connector_category_name"),
    )
    op.create_index(
        "ix_connector_category_published_sort_order",
        "connector_category",
        ["published", "sort_order"],
    )

    op.add_column("skill", sa.Column("category_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_skill_category_id",
        "skill",
        "skill_category",
        ["category_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("mcp_server_catalog", sa.Column("category_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_mcp_server_catalog_category_id",
        "mcp_server_catalog",
        "connector_category",
        ["category_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # The categories go with the columns. Nothing else pointed at them, and
    # both catalogs read exactly as they did before: uncategorised.
    op.drop_constraint(
        "fk_mcp_server_catalog_category_id", "mcp_server_catalog", type_="foreignkey"
    )
    op.drop_column("mcp_server_catalog", "category_id")
    op.drop_constraint("fk_skill_category_id", "skill", type_="foreignkey")
    op.drop_column("skill", "category_id")

    op.drop_index("ix_connector_category_published_sort_order", table_name="connector_category")
    op.drop_table("connector_category")
    op.drop_index("ix_skill_category_published_sort_order", table_name="skill_category")
    op.drop_table("skill_category")
