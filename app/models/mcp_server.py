"""mcp_server: a user-created Connector — one remote MCP server config.

Backs the "add your own Connector" feature (like Claude Connectors). A row
carries the picker metadata (``name``), the endpoint (``url``), how to
authenticate (``auth_type`` + Fernet-encrypted ``encrypted_credential``), and a
cached tool listing for display. The worker resolves url + credential live by
``id`` at turn time — the credential is never snapshotted onto ``session_turn``.

Always user-owned: every row has a ``user_id``, and its own ``connected`` flag
is the on/off switch (no separate preference table — that existed for skills
only to override *shared* rows). Connectors offered in the platform catalog
are **copied** onto a row of the user's own at connect time
(``catalog_id`` points back), never shared, because everything that makes a
Connector usable — tokens, credentials, the switch — is per-user anyway.
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
    # The catalog entry this row was copied from; NULL for a Connector the
    # user defined themselves. Clients join the catalog on it to tell an entry
    # they have already connected from one still on offer. ON DELETE SET NULL:
    # retiring an entry leaves existing Connectors working as custom ones, since
    # url / auth_type / credentials all live here, not on the template.
    catalog_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("mcp_server_catalog.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    auth_type: Mapped[MCPServerAuthType] = mapped_column(
        SAEnum(
            MCPServerAuthType,
            name="mcp_server_auth_type",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
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
    # authorizes, then rotated in place by the background token refresher — the
    # only thing allowed to spend a refresh token. ``oauth_scope``: the
    # requested scope string. Credentials are never returned to the client.
    oauth_client_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_tokens_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_scope: Mapped[str | None] = mapped_column(String, nullable=True)
    # The authorization server's metadata and the RFC 9728 resource document,
    # captured when the user authorized. The token refresher reads this to know
    # where to send a rotation and what its RFC 8707 ``resource`` must name;
    # without it there is nothing safe to guess from, so such a row is left
    # alone. Cached rather than rediscovered per rotation: these were just
    # fetched during authorization and barely ever change, and a provider that
    # does move an endpoint makes the rotation fail, which asks the user to
    # reauthorize and rewrites this. Not encrypted — public discovery documents.
    oauth_metadata: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True,
    )
    # When the stored access token stops being valid. Absolute, because
    # ``OAuthToken.expires_in`` is relative to an issuance moment the token
    # itself doesn't carry. This is what the token refresher selects on, so a
    # NULL means a grant it never looks at — which is right for the two cases
    # that produce one: rows authorized before this column existed, and grants
    # nothing can renew (no ``expires_in`` *and* no refresh token). A renewable
    # grant that states no lifetime gets an assumed one instead; see
    # ``services.mcp.grant._deadline_for``.
    oauth_tokens_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    # Cached at create / re-probe so the UI can show "connected · N tools"
    # without a live round-trip. The worker re-lists tools live each turn —
    # this is display-only and may lag the server.
    tool_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"),
    )
    tools_preview: Mapped[list[dict[str, str]] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True,
    )
    # What the server advertises for itself in ``serverInfo.icons`` (SEP-973),
    # captured at probe time. The UI prefers a curated catalog icon over this,
    # and falls back to the built-in glyph when neither exists.
    advertised_icon_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Bare server-side names of the tools the user switched off; everything else
    # the server exposes stays available. Exceptions rather than a selection, so
    # a tool the server adds later is available at once and one it drops leaves
    # an inert entry. Not folded into ``tools_preview`` — that is overwritten
    # wholesale on every re-probe and would erase the switches.
    disabled_tools: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list,
    )
    # Whether the user currently has this Connector connected — i.e. whether its
    # tools are handed to the agent each turn. Distinct from ``authorized``
    # (OAuth tokens present): a Connector can hold valid tokens and still be
    # disconnected, which is the point — disconnecting keeps the credentials so
    # reconnecting costs nothing, whereas deleting throws them away.
    connected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"),
    )
    # Set by the token refresher when the authorization server refuses the
    # refresh token itself — revoked at the provider, expired, or a rotated one
    # rejected. Never by the worker: a turn can see that its access token was
    # refused, not that the grant behind it is dead. Kept apart
    # from ``connected`` on purpose: that is the user's switch, this is the
    # provider's verdict, and only the pair distinguishes "I turned it off" from
    # "it needs reconnecting". Cleared on the next successful connect, so a
    # transient misread heals itself. ``oauth_``-prefixed like its siblings —
    # only the OAuth path can set it.
    oauth_reauthorization_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
    )
    # Set by the worker when the most recent turn that tried could not reach
    # this Connector at all — DNS gone, TLS refused, 502, handshake timeout.
    # The sibling of the flag above, and the only health signal a non-OAuth
    # Connector can have: that one is a verdict on an OAuth *grant*, so it can
    # never fire for BEARER / HEADER / NONE. Both cleared on the next
    # successful connect; the reason stays in the worker log rather than a
    # free-text column that would leak an upstream server's internals.
    last_connect_failed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
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
