"""mcp_server: a user-created Connector — one remote MCP server config.

Backs the "add your own Connector" feature (like Claude Connectors). A row
carries the picker metadata (``name``), the endpoint (``url``), how to
authenticate (``auth_type`` + Fernet-encrypted ``encrypted_credential``), and a
cached tool listing for display. The worker resolves url + credential live by
``id`` at turn time — the credential is never snapshotted onto ``session_turn``.

v1 is user-owned only: every row has a ``user_id`` (no platform/OFFICIAL
directory yet), and its own ``enabled`` flag is the on/off switch (no separate
preference table — that existed for skills only to override *shared* rows).
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    TIMESTAMP,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class MCPServerAuthType(StrEnum):
    NONE = "NONE"
    BEARER = "BEARER"
    HEADER = "HEADER"
    OAUTH = "OAUTH"


class MCPServer(Base):
    __tablename__ = "mcp_server"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
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
    # Fernet ciphertext of the credential: the bearer token (BEARER), or a
    # JSON ``{"header": name, "value": val}`` (HEADER). NULL when auth_type is
    # NONE. Never returned to the client (omitted from ConnectorResponse).
    encrypted_credential: Mapped[str | None] = mapped_column(Text, nullable=True)
    # OAuth (auth_type=OAUTH) — all NULL for the other auth types.
    # ``oauth_client_encrypted``: Fernet JSON of the SDK ``OAuthClientInformationFull``
    # (client_id/secret, from DCR or manual). ``oauth_tokens_encrypted``: Fernet
    # JSON of the SDK ``OAuthToken`` (access/refresh/expiry); NULL until the user
    # authorizes, then refreshed in place by the worker. ``oauth_scope``: the
    # requested scope string. Credentials are never returned to the client.
    oauth_client_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_tokens_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_scope: Mapped[str | None] = mapped_column(String, nullable=True)
    # Cached at create / re-probe so the UI can show "connected · N tools"
    # without a live round-trip. The worker re-lists tools live each turn —
    # this is display-only and may lag the server.
    tool_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"),
    )
    tools_preview: Mapped[list[dict[str, str]] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_mcp_server_user_id_name"),
    )

    def __repr__(self) -> str:
        return f"<MCPServer(id={self.id}, user_id={self.user_id}, name={self.name!r})>"
