"""Schema validation for the /api/connectors surface."""

import pytest
from pydantic import ValidationError

from app.models.mcp_server import MCPServerAuthType
from app.schemas.mcp_server import ConnectorCreate, ConnectorResponse, ConnectorUpdate


class TestResponseHidesCredential:
    def test_credential_not_exposed(self):
        assert "encrypted_credential" not in ConnectorResponse.model_fields
        assert "credential" not in ConnectorResponse.model_fields


class TestCreateAuthValidation:
    def test_none_needs_no_credential(self):
        c = ConnectorCreate(name="gh", url="https://mcp.gh.dev/x")
        assert c.auth_type == MCPServerAuthType.NONE

    def test_bearer_requires_credential(self):
        with pytest.raises(ValidationError):
            ConnectorCreate(name="gh", url="https://x/y", auth_type="BEARER")

    def test_header_requires_name_and_value(self):
        with pytest.raises(ValidationError):
            ConnectorCreate(
                name="gh",
                url="https://x/y",
                auth_type="HEADER",
                credential="k",
            )
        ok = ConnectorCreate(
            name="gh",
            url="https://x/y",
            auth_type="HEADER",
            credential="k",
            header_name="X-Api-Key",
        )
        assert ok.header_name == "X-Api-Key"


class TestCreateSourceValidation:
    """A Connector comes from exactly one source: the catalog or a custom URL."""

    def test_catalog_id_alone_is_valid(self):
        c = ConnectorCreate(catalog_id=3)
        assert c.catalog_id == 3 and c.name is None and c.url is None

    def test_neither_source_rejected(self):
        with pytest.raises(ValidationError):
            ConnectorCreate()

    def test_url_without_name_rejected(self):
        with pytest.raises(ValidationError):
            ConnectorCreate(url="https://mcp.gh.dev/x")

    @pytest.mark.parametrize(
        "extra",
        [
            {"name": "gh"},
            {"url": "https://mcp.gh.dev/x"},
            {"auth_type": "BEARER", "credential": "tok"},
            {"oauth_scope": "read"},
        ],
    )
    def test_catalog_id_rejects_fields_the_entry_owns(self, extra):
        # Silently ignoring them would let a caller think they overrode the
        # official definition.
        with pytest.raises(ValidationError):
            ConnectorCreate(catalog_id=3, **extra)


class TestUpdateAuthValidation:
    def test_auth_untouched_when_type_omitted(self):
        u = ConnectorUpdate(name="renamed")  # no auth_type → no credential needed
        assert u.auth_type is None

    def test_bearer_change_requires_credential(self):
        with pytest.raises(ValidationError):
            ConnectorUpdate(auth_type="BEARER")

    def test_credential_without_auth_type_rejected(self):
        # Silently ignoring a credential rotation would be a footgun.
        with pytest.raises(ValidationError):
            ConnectorUpdate(credential="newtok")
