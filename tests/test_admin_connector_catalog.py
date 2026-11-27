"""The catalog rules the console enforces at its request schemas.

Separate from ``test_admin_connector_secrets.py``, which pins the one
invariant that surface exists to protect (no secret is representable).
These are the ordinary rules of adding an entry: what a URL must be —
it is the entry's identity — and what an entry looks like on arrival.
"""

import pytest
from pydantic import ValidationError

from app.models.mcp_server import MCPServerAuthType
from app.schemas.admin.connector import (
    ConnectorCatalogEntryCreate,
    ConnectorCatalogEntryUpdate,
)
from app.services.connector.catalog import INSTANTIABLE_AUTH_TYPES


def _create(**over) -> ConnectorCatalogEntryCreate:
    return ConnectorCatalogEntryCreate(
        **{
            "name": "Notion",
            "url": "https://mcp.notion.com/mcp",
            **over,
        }
    )


def test_the_console_cannot_reintroduce_a_key() -> None:
    """What identifies an entry is settled at the table
    (``TestWhatIdentifiesAnEntry`` in ``test_connector_catalog.py``);
    this is the console keeping its side of it."""
    assert "key" not in ConnectorCatalogEntryCreate.model_fields
    assert "key" not in ConnectorCatalogEntryUpdate.model_fields


def test_the_identity_is_editable() -> None:
    """Which is why ``update_catalog_entry`` checks for a duplicate URL
    at all: while the identity was an immutable ``key`` no edit could
    collide, and the check would have looked like dead code."""
    assert "url" in ConnectorCatalogEntryUpdate.model_fields


def test_an_entry_can_arrive_already_filed() -> None:
    """The add form offers the tab, so an operator does not save an entry
    and then immediately edit it to put it on one. That the id names a
    real category is checked by the catalog service
    (``catalog.create_entry``), not here — the schema cannot see the table."""
    assert _create(category_id=7).category_id == 7


def test_an_entry_arrives_uncategorised_unless_told_otherwise() -> None:
    """The same state every entry created before the taxonomy existed
    carries, so omitting the field is not a special case."""
    assert _create().category_id is None


def test_an_entry_arrives_as_a_draft() -> None:
    """Publishing is the separate PATCH, which is what makes "check the
    URL and icon first" a step rather than advice."""
    assert _create().published is False


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp",  # the typo that matters
        "ftp://mcp.example.com",
        "mcp.example.com",
        "",
    ],
)
def test_a_url_that_is_not_https_is_refused(url: str) -> None:
    """``url_guard.validate_public_url`` requires https at connect time,
    so an ``http`` entry would be accepted here and then fail for every
    user who added it. Refused at the schema instead — on the edit path
    too, since that is the other way a bad URL gets in."""
    with pytest.raises(ValidationError):
        _create(url=url)

    with pytest.raises(ValidationError):
        ConnectorCatalogEntryUpdate(url=url)


_CANONICAL = "https://mcp.notion.com/mcp"


@pytest.mark.parametrize(
    "typed",
    [
        "https://MCP.Notion.com/mcp",  # host case
        "https://mcp.notion.com:443/mcp",  # the default port
        "https://mcp.notion.com/mcp#setup",  # a fragment, never sent
        "  https://mcp.notion.com/mcp  ",
    ],
)
def test_the_console_stores_the_canonical_spelling(typed: str) -> None:
    """Both constraints policing this column match on the exact string,
    so a spelling only the console could write is a duplicate neither
    would catch. The edit path normalizes too — it is the other way one
    gets in."""
    assert _create(url=typed).url == _CANONICAL
    assert ConnectorCatalogEntryUpdate(url=typed).url == _CANONICAL


def test_the_scheme_is_judged_after_normalizing() -> None:
    """Which is why the constraints sit after the normalizer in the
    annotation: ``HTTPS://`` is ``https://`` in a spelling no server can
    tell apart, and the other order would refuse a usable entry over
    letter case."""
    assert _create(url="HTTPS://mcp.notion.com/mcp").url == _CANONICAL


def test_the_address_check_is_left_to_connect_time() -> None:
    """Only the scheme is checked here. The address-level SSRF defence
    lives in ``url_guard.guarded_client``, which resolves *and pins* the
    address at connect time — repeating the lookup here would refuse an
    entry staged before its service is live, and would still leave the
    TOCTOU window that only pinning closes."""
    entry = _create(url="https://not-live-yet.example.com/mcp")

    assert entry.url == "https://not-live-yet.example.com/mcp"


@pytest.mark.parametrize("auth_type", sorted(INSTANTIABLE_AUTH_TYPES))
def test_an_instantiable_auth_type_is_accepted(auth_type) -> None:
    assert _create(auth_type=auth_type).auth_type is auth_type
    assert ConnectorCatalogEntryUpdate(auth_type=auth_type).auth_type is auth_type


@pytest.mark.parametrize("auth_type", [MCPServerAuthType.BEARER, MCPServerAuthType.HEADER])
def test_an_auth_type_no_user_could_add_is_refused(auth_type) -> None:
    """``build_connector`` refuses these — they need a per-user secret the
    catalog cannot hold — so an entry declaring one is unusable the moment
    it is published. ``catalog.py`` says operator tooling should offer only
    ``INSTANTIABLE_AUTH_TYPES`` and ``manage_connector_catalog.py`` already
    limits its ``--auth-type`` choices; the console must not be the one
    surface that lets it through."""
    assert auth_type not in INSTANTIABLE_AUTH_TYPES

    with pytest.raises(ValidationError):
        _create(auth_type=auth_type)

    with pytest.raises(ValidationError):
        ConnectorCatalogEntryUpdate(auth_type=auth_type)


def test_an_absent_auth_type_still_means_leave_alone() -> None:
    """The update schema documents ``None`` as "leave this field alone",
    so an explicit ``null`` must pass the instantiable check rather than
    trip it."""
    # A carrier field is needed now that nothing else is required — an
    # entirely empty update is refused by ``_at_least_one_change``.
    assert ConnectorCatalogEntryUpdate(name="Notion").auth_type is None
    assert ConnectorCatalogEntryUpdate(name="Notion", auth_type=None).auth_type is None


def test_an_empty_update_is_refused() -> None:
    """Same guard as the skill PATCH: nothing is required on its own, so
    the model has to refuse a body that asks for nothing."""
    with pytest.raises(ValidationError):
        ConnectorCatalogEntryUpdate()
