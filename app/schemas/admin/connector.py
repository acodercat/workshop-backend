"""Request / response models for the console's Connector surface.

**No secret is representable here.** ``encrypted_credential``,
``oauth_tokens_encrypted`` and ``oauth_client_secret_encrypted`` are
absent from every model below — not masked, not optional, absent. A
field that does not exist cannot be leaked by a later "just add the
column" change, and the console never needs one: it publishes catalog
entries and answers "why is this connector failing", neither of which
requires reading a credential.

Naming follows the repo's Connector/MCP split: this is what operators
manage, so it is ``Connector*`` throughout, even though the tables are
the legacy ``mcp_server`` / ``mcp_server_catalog``.
"""

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

from app.models.mcp_server import MCPServerAuthType
from app.services.connector.catalog import INSTANTIABLE_AUTH_TYPES
from app.services.connector.url_identity import normalize_connector_url

# Every URL that reaches a column goes through here, so the two spellings of
# one endpoint cannot become two entries — see ``url_identity``. On the type
# rather than at the call sites that write one, matching
# ``ConnectorEndpointUrl`` on the public surface.
# The constraints sit *after* the normalizer so they judge what will be
# stored: a URL only over the limit because of a ``:443`` should not be
# refused, and ``HTTPS://`` is ``https://`` in a spelling no server can tell
# apart, so the pattern should accept it rather than reject the entry.
ConnectorUrl = Annotated[
    str,
    AfterValidator(normalize_connector_url),
    Field(min_length=1, max_length=2048, pattern=r"^https://"),
]
"""A catalog entry's endpoint, and its identity.

Two entries pointing at one server are the same Connector offered
twice, whatever they are named, so this is the column carrying the
table's unique constraint (``uq_mcp_server_catalog_url``). ``name`` is
deliberately not unique: it is the copy users read, and renaming one
entry must not collide with another.

Canonical, because both constraints that police it match on the exact
string: this one, and ``uq_mcp_server_user_id_url`` on the rows users
add from these entries. A spelling only the console could write is a
duplicate neither would catch.

``https`` is required here because
:func:`~app.services.mcp.url_guard.validate_public_url` requires it at
connect time — an ``http://`` entry would be accepted into the catalog
and then fail for every user who added it. That is the whole check this
performs: a scheme test costs nothing, while the address-level SSRF
defence stays at connect time, where ``guarded_client`` pins the
resolved IP and closes the TOCTOU window a create-time lookup would
leave open. Resolving DNS here would also refuse an entry staged before
its service is live, which is a normal thing to want.
"""


class ConnectorCatalogEntryResponse(BaseModel):
    """One catalog entry, published or not.

    ``has_preregistered_client`` reports *whether* the platform holds an
    OAuth app for this entry without exposing any part of it — enough
    for an operator to know why a connector authorizes without dynamic
    registration, and nothing more.
    """

    id: int
    name: str
    url: str
    auth_type: MCPServerAuthType
    description: str | None
    icon_url: str | None
    oauth_scope: str | None
    has_preregistered_client: bool
    published: bool
    sort_order: int
    # The tab this entry browses under on the connectors page, or
    # ``None`` while it is uncategorised.
    category_id: int | None
    created_at: datetime
    updated_at: datetime


def _must_be_instantiable(
    value: MCPServerAuthType | None,
) -> MCPServerAuthType | None:
    """Reject an auth type no user could ever add the entry with.

    ``None`` passes through: on the update schema that is the documented
    "leave this field alone", so an explicit ``null`` in the body has to
    mean the same rather than fail.

    ``BEARER``/``HEADER`` need a per-user secret and the catalog holds
    none, so :func:`~app.services.connector.catalog.build_connector` refuses
    them — an entry declaring one is unusable the moment it is published.
    ``catalog.py`` says operator tooling should offer only
    ``INSTANTIABLE_AUTH_TYPES``; ``manage_connector_catalog.py`` already
    limits its ``--auth-type`` choices to them, and this is the console
    obeying the same rule from the same constant.
    """
    if value is not None and value not in INSTANTIABLE_AUTH_TYPES:
        allowed = ", ".join(sorted(auth_type.value for auth_type in INSTANTIABLE_AUTH_TYPES))
        raise ValueError(
            f"{value.value} needs a per-user credential the catalog cannot "
            f"hold; a catalog entry may be {allowed}"
        )
    return value


class ConnectorCatalogEntryCreate(BaseModel):
    """A new catalog entry.

    Deliberately no OAuth client credentials: seeding a preregistered
    app means sealing a secret, which stays in
    ``scripts/manage_connector_catalog.py`` so this surface handles none.
    """

    name: str = Field(min_length=1, max_length=64)
    url: ConnectorUrl
    auth_type: MCPServerAuthType = MCPServerAuthType.NONE
    description: str | None = Field(default=None, max_length=2000)
    icon_url: str | None = Field(default=None, max_length=2048)
    oauth_scope: str | None = Field(default=None, max_length=512)
    published: bool = False
    sort_order: int = 0
    # Optional, and omitting it files the entry as uncategorised — the
    # same state the column carries for every entry created before the
    # taxonomy existed. Here so the console's add form does not have to
    # save an entry and then immediately edit it to put it on a tab.
    category_id: int | None = None

    _check_auth_type = field_validator("auth_type")(_must_be_instantiable)


class ConnectorCatalogEntryUpdate(BaseModel):
    """A partial edit. Absent fields are left alone.

    ``None`` means "leave alone", not "clear" — so the nullable
    ``description`` / ``icon_url`` cannot be emptied through this
    surface. Distinguishing the two needs a sentinel, and nobody has
    wanted to blank one yet; add it the day someone does rather than
    carrying the ambiguity now. ``category_id`` is the one exception,
    and says why below.
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    url: ConnectorUrl | None = None
    # Editable, following the public surface, where a user may change
    # their own Connector's auth. A catalog entry carries no per-user
    # credential, so unlike there it is the one field with nothing to
    # keep coherent alongside it.
    auth_type: MCPServerAuthType | None = None
    description: str | None = Field(default=None, max_length=2000)
    icon_url: str | None = Field(default=None, max_length=2048)
    oauth_scope: str | None = Field(default=None, max_length=512)
    published: bool | None = None
    sort_order: int | None = None
    # The exception to the rule above: an explicit ``null`` clears the
    # category. Retiring one, or correcting an entry filed under the
    # wrong one, are ordinary operator moves, and unlike a description
    # there is a real "no category" state to return an entry to.
    # ``update_catalog_entry`` reads ``model_fields_set`` to tell an
    # explicit ``null`` from an absent field.
    category_id: int | None = None

    _check_auth_type = field_validator("auth_type")(_must_be_instantiable)

    @model_validator(mode="after")
    def _at_least_one_change(self) -> "ConnectorCatalogEntryUpdate":
        """Refuse a request that asks for nothing.

        Same guard, same phrasing as :class:`~app.schemas.admin.skill.SkillUpdate`
        — keyed on ``model_fields_set`` so ``{"category_id": null}`` reads as
        the clearing it is, which is also what ``update_catalog_entry`` keys
        its own explicit-``null`` handling on, so the two cannot disagree
        about what counts as a change.
        """
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to change.")
        return self


class ConnectorResponse(BaseModel):
    """One user's configured Connector, read-only.

    Health, not configuration: an operator answering "why is this
    failing" needs the connection state and tool count, never the
    credential behind it.
    """

    id: int
    user_id: int
    # The identity every console table renders together: a login with
    # the person's display name under it, both off the join the page
    # already makes.
    owner_username: str
    owner_name: str
    name: str
    url: str
    auth_type: MCPServerAuthType
    connected: bool
    last_connect_failed: bool
    oauth_reauthorization_required: bool
    tool_count: int
    disabled_tool_count: int
    catalog_id: int | None
    created_at: datetime
    updated_at: datetime
