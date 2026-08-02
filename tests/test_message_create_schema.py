"""Validation for ``MessageCreate`` — a turn needs text or a file.

The composer diverts a huge paste to a ``.txt`` attachment, leaving the
typed text empty; an attachments-only send is legal (the file is the
content). Only the truly-empty case (no text, no files) is rejected.
"""

import pytest
from pydantic import ValidationError

from app.schemas.session import FileRef, MessageCreate


def _file() -> FileRef:
    return FileRef(
        file_key="k", filename="pasted_text1.txt", size_bytes=1,
    )


def test_text_only_is_valid() -> None:
    msg = MessageCreate(content="hello")
    assert msg.content == "hello"


def test_attachments_only_is_valid() -> None:
    # No typed text, one file — the paste-to-file / attach-a-doc case.
    msg = MessageCreate(content="", files=[_file()])
    assert msg.content == ""
    assert len(msg.files) == 1


def test_content_is_stripped() -> None:
    assert MessageCreate(content="  hi  ").content == "hi"


def test_empty_text_and_no_files_rejected() -> None:
    with pytest.raises(ValidationError):
        MessageCreate(content="")


def test_whitespace_only_and_no_files_rejected() -> None:
    # Stripped to empty, no files → nothing to act on.
    with pytest.raises(ValidationError):
        MessageCreate(content="   \n  ")
