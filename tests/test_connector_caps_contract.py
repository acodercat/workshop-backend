"""The backend's half of the Connector tool-catalog caps, pinned to literals.

Two numbers bound what an MCP server's catalog may contain, and they are
declared twice: here as ``MAX_DISABLED_TOOLS`` / ``MAX_TOOL_NAME_LEN`` in
``app/schemas/mcp_server.py``, and in the worker as ``_MAX_LISTED_TOOLS`` /
``_MAX_LISTED_NAME_LENGTH`` in ``agent_worker/runtime/connectors.py``.

**They are not two independent limits that happen to match — they are one
limit, and a drift has a direction that is a security bug.** This side's probe
stores the tool listing the Connectors page renders, and
``mcp_server.disabled_tools`` is how a user switches a tool off — so a tool
dropped here is one the user can neither see nor disable. Tightening these
numbers without tightening the worker's leaves that tool still handed to the
model: callable, invisible, and with no way to refuse it.

**The order the caps are applied in is part of the contract too**, and no
assertion here can reach it. Both sides trim on the server's own ordering
before any size limit, so both pick the same tools; trim by size first on one
side and each ends up with a different set — which is how a tool the user was
never shown becomes callable while both counts still read 256.

Same shape as ``tests/test_redis_key_contract.py``. The mirror is
``agent-worker/tests/runtime/test_connector_caps_contract.py``.
"""

from app.schemas.mcp_server import MAX_DISABLED_TOOLS, MAX_TOOL_NAME_LEN

#: The agreement. Mirrored in the worker file named above.
AGREED_MAX_TOOLS = 256
AGREED_MAX_NAME_LENGTH = 128


def test_the_tool_count_cap_is_the_agreed_literal():
    assert MAX_DISABLED_TOOLS == AGREED_MAX_TOOLS, (
        f"this service now bounds the listing at {MAX_DISABLED_TOOLS} where "
        f"the contract says {AGREED_MAX_TOOLS}. The worker admits tools into "
        f"the model's catalog with _MAX_LISTED_TOOLS in "
        f"agent_worker/runtime/connectors.py — lowering this alone leaves the "
        f"difference callable by the model and invisible to the user. Change "
        f"it there in the same breath, and its mirror test with it."
    )


def test_the_tool_name_cap_is_the_agreed_literal():
    assert MAX_TOOL_NAME_LEN == AGREED_MAX_NAME_LENGTH, (
        f"this service now bounds a tool name at {MAX_TOOL_NAME_LEN} where the "
        f"contract says {AGREED_MAX_NAME_LENGTH}. A name past this can never "
        f"appear in disabled_tools, so it has no off switch; the worker admits "
        f"names with _MAX_LISTED_NAME_LENGTH and must agree."
    )
