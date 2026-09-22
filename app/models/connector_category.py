"""connector_category: how the Connector catalog is grouped for browsing.

One row is a tab on the connectors page — the strip that follows the pinned
「个人」 tab. Only catalog templates are grouped: a user's own ``mcp_server``
rows live behind 「个人」 and are not the platform's to classify.

Named for the Connector, not for the table it is pointed at from. The rows
carrying ``category_id`` live in the legacy-named ``mcp_server_catalog``, but
new schema serving Connectors is ``connector*`` — see the Connector vs MCP
note in ``CLAUDE.md``.

Separate from ``skill_category`` on purpose; the reasoning lives in that
module's docstring and applies symmetrically here.
"""

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class ConnectorCategory(Base):
    __tablename__ = "connector_category"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    # The tab's label, and what every locale falls back to. Unique because
    # there is no slug: this is what an operator picks the category by in the
    # console, and two rows reading 「日历」 leave them nothing to choose with.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Optional ``{locale: name}`` overrides. NULL when the category reads the
    # same everywhere — the column above is what every locale falls back to, so
    # nothing has to special-case an untranslated row.
    # ``none_as_null`` so "no translations" persists as SQL NULL rather than a
    # JSONB ``'null'`` literal, keeping ``IS NULL`` honest.
    name_i18n: Mapped[dict[str, str] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    # Tab visibility, the same word and the same reason as
    # ``mcp_server_catalog.published``: whether a category is offered is the
    # operator's call. Unpublishing hides the tab without deleting the row, so
    # the entries pointing at it keep their foreign key and simply read as
    # uncategorised until it comes back.
    published: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    # Left-to-right order of the tabs after the pinned one.
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # The strip is only ever read published-first in display order.
        Index(
            "ix_connector_category_published_sort_order",
            "published",
            "sort_order",
        ),
        UniqueConstraint("name", name="uq_connector_category_name"),
    )

    def __repr__(self) -> str:
        return f"<ConnectorCategory(id={self.id}, name={self.name!r}, published={self.published})>"
