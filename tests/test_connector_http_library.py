"""The MCP path speaks httpx2, and nothing in it may reach for httpx.

Two libraries live in this project. `httpx2` is the MCP SDK's since 2.x, and
therefore the SSRF-guarded outbound path's; plain `httpx` remains only for the
test clients that call *our own* API (``tests/conftest.py``, ``tests/e2e``).

They are duck-compatible across every surface the connector code touches — a
`handle_async_request` override, a `MockTransport`, a `Response` — so mixing them
does not raise. It silently does the wrong thing instead: a request built by one
and sent through the other bypasses `_PinnedResolveTransport`, which is the SSRF
defence, and `tests/` is outside mypy's `files` list so nothing else would catch
it. Hence a test.

The two heavy connector test modules import `httpx2 as httpx` so their ~30 call
sites needed no editing — which is safe only while nothing re-adds the real one
underneath the alias.
"""

import ast
import pathlib

import pytest

#: Everything that builds or sends an outbound MCP/OAuth request, and the tests
#: that drive them. ``conftest`` and ``e2e`` are deliberately absent.
_HTTPX2_ONLY = [
    "app/services/mcp/url_guard.py",
    "app/services/mcp/oauth.py",
    "app/services/mcp/probe.py",
    "tests/test_connector_url_guard.py",
    "tests/test_connector_oauth.py",
    "tests/test_connector_probe.py",
]

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _imported_modules(path: pathlib.Path) -> set[str]:
    """Top-level module names this file imports, however it spells the import."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("relative", _HTTPX2_ONLY)
def test_the_connector_path_does_not_import_httpx(relative):
    path = _ROOT / relative
    assert path.exists(), f"{relative} moved; this guard has to move with it"

    assert "httpx" not in _imported_modules(path), (
        f"{relative} imports httpx. The MCP path is httpx2's since the SDK "
        f"bump, and the two do not raise when mixed — a request that crosses "
        f"between them just misses the DNS-pinning transport. Import httpx2 "
        f"(the two test modules alias it as ``httpx`` so their call sites read "
        f"unchanged)."
    )


def test_the_alias_is_what_those_test_modules_actually_use():
    # The parametrized check above passes for a file that imports neither, so
    # on its own it would also pass if someone deleted the alias and left the
    # ~30 ``httpx.`` call sites resolving to nothing. Assert the substitution
    # is really in place.
    for relative in (
        "tests/test_connector_url_guard.py",
        "tests/test_connector_oauth.py",
    ):
        source = (_ROOT / relative).read_text()
        assert "import httpx2 as httpx" in source, f"{relative} lost the httpx2 alias"
