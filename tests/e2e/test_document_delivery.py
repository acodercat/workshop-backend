"""End-to-end test: agent generates a document and delivers it via OSS.

For each of PPTX / DOCX / PDF, drive the agent to:

1. Build the file inside the sandbox using whatever tooling the
   sandbox image carries (``pptxgenjs``, ``python-docx`` / libreoffice,
   ``pdflatex`` / libreoffice — see the worker's
   ``SANDBOX_DESCRIPTION`` for what's pre-installed).
2. Hand it off to the user via the framework's ``deliver_file`` tool,
   which the worker translates into an OSS upload + a
   :class:`agent_worker.domain.files.FileAsset` riding on the
   ``agent.tool_result.files`` field.

Verifies, in order:

- **SSE event correctness**: clean envelope (status: started → ... →
  stopped without ``stop_reason="error"``), and every
  ``agent.tool_use`` paired with a matching ``agent.tool_result`` by
  ``tool_use_id``.
- **deliver_file actually fired**: at least one tool_use carries
  ``tool == "deliver_file"`` and its result lists a file with the
  expected extension.
- **The file landed in OSS**: presign a download URL for the file_key
  and range-fetch the first 16 bytes; assert they start with the
  format's magic bytes (``PK`` for the zip-based PPTX/DOCX, ``%PDF-``
  for PDF). This is the only part of the test that proves the bytes
  the agent generated actually round-tripped through the worker's OSS
  uploader to a URL the frontend would download from.
- **Persisted state + cleanup**: session ends in ``COMPLETE`` with
  USER+ASSISTANT messages, then DELETE returns 204 and a follow-up
  GET 404s.

Skipped unless ``RUN_E2E_TESTS=1`` is set; preconditions documented in
``backend/CLAUDE.md``.
"""

import json

import httpx
import pytest

from tests.conftest import requires_e2e
from tests.e2e._helpers import (
    assert_event_envelope_clean,
    assert_tool_calls_paired,
    consume_sse_until_stopped,
)

# (extension, magic-byte prefix, prompt)
# Magic bytes:
#  - PPTX/DOCX are ZIP containers → ``PK\x03\x04``
#  - PDF starts with the literal ``%PDF-`` header
# Prompts explicitly name the format + filename + the deliver_file
# step so the agent doesn't free-associate the workflow. We don't
# dictate the *tool* (let the agent pick pptxgenjs / python-docx /
# libreoffice / pdflatex / etc.) — the test verifies the outcome.
_DOCUMENT_CASES = [
    pytest.param(
        "pptx",
        b"PK\x03\x04",
        "请创建一个简单的 PPT 文件（仅一页幻灯片，标题为 'Hello E2E'），"
        "保存为 hello.pptx，然后调用 deliver_file 工具交付给我。",
        id="pptx",
    ),
    pytest.param(
        "docx",
        b"PK\x03\x04",
        "请创建一个简单的 Word 文档（内容包含 'Hello E2E Test'），"
        "保存为 hello.docx，然后调用 deliver_file 工具交付给我。",
        id="docx",
    ),
    pytest.param(
        "pdf",
        b"%PDF-",
        "请创建一个简单的 PDF 文件（内容包含 'Hello E2E Test'），"
        "保存为 hello.pdf，然后调用 deliver_file 工具交付给我。",
        id="pdf",
    ),
]


# Document generation involves multi-tool agent loops (write code →
# run interpreter → inspect output → fix → deliver), each turn taking
# several seconds. 10 minutes is generous for a slow LLM / cold sandbox.
_DOC_SSE_TIMEOUT_SECONDS = 600.0


def _flatten_delivered_files(
    events: list[tuple[str, dict]],
) -> list[dict]:
    """Pull every ``FileAsset`` dict out of every ``agent.tool_result``.

    The wire shape stores ``files`` as a JSON-encoded string per the
    worker's ``_encode_fields`` (Redis Streams hold flat str→str maps).
    Empty results land as ``"[]"`` or ``"null"`` — both filter out.
    """
    delivered: list[dict] = []
    for type_, payload in events:
        if type_ != "agent.tool_result":
            continue
        files_json = payload.get("files")
        if not files_json or files_json == "null":
            continue
        parsed = json.loads(files_json)
        if parsed:
            delivered.extend(parsed)
    return delivered


@requires_e2e
@pytest.mark.parametrize("ext, magic, prompt", _DOCUMENT_CASES)
async def test_document_generation_and_delivery(
    http_client: httpx.AsyncClient,
    test_user: dict,
    ext: str,
    magic: bytes,
    prompt: str,
) -> None:
    # 1. Session + message
    resp = await http_client.post(
        "/sessions",
        json={"title": f"E2E {ext} delivery"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    resp = await http_client.post(
        f"/sessions/{session_id}/messages",
        json={"content": prompt},
    )
    assert resp.status_code == 201, resp.text

    # 2. Drain SSE until terminal
    events = await consume_sse_until_stopped(
        http_client,
        session_id,
        timeout=_DOC_SSE_TIMEOUT_SECONDS,
    )

    # 3. Wire-shape contracts that hold for every well-formed turn.
    assert_event_envelope_clean(events)
    assert_tool_calls_paired(events)

    # 4. The agent must have called deliver_file at least once.
    deliver_calls = [
        payload
        for type_, payload in events
        if type_ == "agent.tool_use" and payload.get("tool") == "deliver_file"
    ]
    called_tools = [p.get("tool") for t, p in events if t == "agent.tool_use"]
    assert deliver_calls, f"agent did not invoke deliver_file; tools called: {called_tools}"

    # 5. At least one delivered file matches the requested extension.
    delivered_files = _flatten_delivered_files(events)
    matching = [f for f in delivered_files if f.get("filename", "").lower().endswith(f".{ext}")]
    assert matching, (
        f"no delivered .{ext} file found; "
        f"delivered={[f.get('filename') for f in delivered_files]}, "
        f"tools_called={called_tools}"
    )

    # 6. The file actually exists in OSS — presign a download URL and
    #    range-fetch the first 16 bytes to verify the format's magic
    #    bytes. This is the part that proves the worker's OSS upload
    #    + key shape + presign path all line up.
    target = matching[0]
    resp = await http_client.get(
        f"/sessions/{session_id}/files/download-url",
        params={
            "file_key": target["file_key"],
            "filename": target["filename"],
        },
    )
    assert resp.status_code == 200, resp.text
    download_url = resp.json()["download_url"]

    # The presigned URL points at the raw OSS endpoint, not the
    # backend — use a fresh httpx client without the bearer header so
    # OSS doesn't see (and reject) our bearer token in its signed
    # request validation.
    async with httpx.AsyncClient(timeout=30.0) as raw:
        head_resp = await raw.get(
            download_url,
            headers={"Range": "bytes=0-15"},
        )
    # OSS returns 206 for a successful Range request, 200 for a full
    # GET on a small object — either is fine here.
    assert head_resp.status_code in (200, 206), (
        f"OSS GET failed: {head_resp.status_code} {head_resp.text!r}"
    )
    assert head_resp.content.startswith(magic), (
        f"{ext} file at {target['file_key']!r} does not start with "
        f"magic {magic!r}; got {head_resp.content[:16]!r}"
    )

    # 7. Persisted state + delete
    resp = await http_client.get(f"/sessions/{session_id}")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["status"] == "COMPLETE", detail["status"]
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["USER", "ASSISTANT"], roles

    resp = await http_client.delete(f"/sessions/{session_id}")
    assert resp.status_code == 204, resp.text
    resp = await http_client.get(f"/sessions/{session_id}")
    assert resp.status_code == 404
