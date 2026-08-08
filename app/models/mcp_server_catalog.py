"""mcp_server_catalog: the platform's catalog of official Connectors.

One row is a **template**, not a usable connection: the endpoint (``url``), how
to authenticate (``auth_type`` + ``oauth_scope``) and the picker metadata we
show in the catalog. Adding one instantiates an ordinary user-owned
``mcp_server`` row copied from it (``mcp_server.catalog_id`` points back).

Templates rather than shared rows because almost everything a live Connector
carries is per-user — OAuth tokens, credentials, the ``connected`` switch, the
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
    # Copied onto the user's row at add time, and free to repeat: what stops
    # the same official Connector being added twice is the URL travelling with
    # it (``uq_mcp_server_user_id_url``), not the label.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # The entry's identity, and the only thing that has to be unique: two rows
    # pointing at one server are the same Connector offered twice, whatever
    # they are called. ``name`` deliberately is not — it is the display copy,
    # and an operator renaming one entry must not collide with another.
    url: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    auth_type: Mapped[MCPServerAuthType] = mapped_column(
        SAEnum(
            MCPServerAuthType,
            name="mcp_server_auth_type",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        server_default=text("'NONE'"),
    )
    oauth_scope: Mapped[str | None] = mapped_column(String, nullable=True)
    # A pre-registered OAuth app, for providers that offer neither dynamic
    # client registration nor CIMD (Figma requires an app created by hand).
    # NULL on entries whose provider can mint a client on demand — the add
    # flow then falls back to DCR/CIMD exactly as before. The id is the app's
    # public identifier and stays plaintext; the secret is Fernet-encrypted
    # under ``mcp_credential_encryption_key``, the same envelope every stored
    # Connector credential uses (``services/crypto.py``).
    #
    # Add time *snapshots* the pair into the user row's
    # ``oauth_client_encrypted``, so rotating it here reaches new adds only:
    # existing Connectors keep their copy until the provider revokes the old
    # app, at which point re-adding from the catalog picks up the new one.
    oauth_client_id: Mapped[str | None] = mapped_column(String, nullable=True)
    oauth_client_secret_encrypted: Mapped[str | None] = mapped_column(
        String, nullable=True,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Operator-set so a new entry needs no frontend deploy; the card falls back
    # to its generic icon when NULL.
    icon_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Catalog visibility. Deliberately its own word: whether an entry is
    # offered is the operator's call, whereas ``mcp_server.connected`` is each
    # user's own switch — one name for both would conflate them.
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
        # The catalog is only ever read published-first in display order.
        Index(
            "ix_mcp_server_catalog_published_sort_order",
            "published",
            "sort_order",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MCPServerCatalogEntry(id={self.id}, name={self.name!r}, "
            f"published={self.published})>"
        )
