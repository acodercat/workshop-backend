"""mcp_server_catalog: the platform's directory of official Connectors.

One row is a **template**, not a usable connection: the endpoint (``url``), how
to authenticate (``auth_type`` + ``oauth_scope``) and the picker metadata we
show in the directory. Adding one instantiates an ordinary user-owned
``mcp_server`` row copied from it (``mcp_server.catalog_id`` points back).

Templates rather than shared rows because almost everything a live Connector
carries is per-user — OAuth tokens, credentials, the ``enabled`` switch, the
cached tool listing. A shared row would have to push all of those into a
per-user table, leaving a "shared" row that is only ever a template anyway.
Instantiating keeps every existing per-user path (authorize, refresh, toggle,
delete, the per-turn snapshot) working untouched.

Operator-managed: rows are seeded / edited from the backend (see
``scripts/manage_connector_catalog.py``), so adding an official Connector needs
no deploy.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Enum as SAEnum,
    Index,
    Integer,
    String,
    Text,
    TIMESTAMP,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base
from app.models.mcp_server import MCPServerAuthType


class MCPServerCatalogEntry(Base):
    __tablename__ = "mcp_server_catalog"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    # Stable slug (``"notion"``) — the frontend's lookup key for a bundled asset
    # or i18n string. Immutable in practice: changing it orphans those lookups.
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Copied onto the user's row at add time, so it also decides the per-user
    # name uniqueness (``uq_mcp_server_user_id_name``) — which is what makes
    # adding the same official Connector twice a clean 409.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    auth_type: Mapped[MCPServerAuthType] = mapped_column(
        SAEnum(
            MCPServerAuthType,
            name="mcp_server_auth_type",
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
        server_default=text("'NONE'"),
    )
    oauth_scope: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Operator-set so a new entry needs no frontend deploy; the card falls back
    # to its generic icon when NULL.
    icon_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Directory visibility. Deliberately NOT called ``enabled``:
    # ``mcp_server.enabled`` already means "this user's on/off switch", and
    # reusing the word for "listed in the directory" would conflate the two.
    # Unpublishing hides the entry without deleting it, so the FK from rows
    # already instantiated stays intact.
    published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"),
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        # The directory is only ever read published-first in display order.
        Index(
            "ix_mcp_server_catalog_published_sort_order",
            "published",
            "sort_order",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MCPServerCatalogEntry(id={self.id}, key={self.key!r}, "
            f"published={self.published})>"
        )
