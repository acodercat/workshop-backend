"""An upload is read only once the request is authorized.

FastAPI reads a declared ``UploadFile`` before any dependency runs, so a declared
one took a whole bundle from anyone. The body is now read inside the handler,
after authentication: an unauthenticated request is refused without its body
being looked at — which is what a body no parser would accept shows.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import skill as admin_skill
from app.api.dependencies import get_db
from app.api.public import skill as public_skill
from app.exception_handlers import install_error_handlers
from app.rate_limit import limiter

_UNPARSEABLE = {
    "content": b"this is not multipart",
    "headers": {"content-type": "multipart/form-data; boundary=x"},
}


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(limiter, "enabled", False)
    app = FastAPI()
    install_error_handlers(app)
    app.dependency_overrides[get_db] = lambda: MagicMock()
    app.include_router(public_skill.router, prefix="/v1")
    app.include_router(admin_skill.router, prefix="/admin")
    return TestClient(app)


@pytest.mark.parametrize("path", ["/v1/skills", "/admin/skills"])
def test_an_upload_is_not_read_before_the_request_is_authorized(client, path):
    response = client.post(path, **_UNPARSEABLE)
    assert response.status_code == 401, response.text
