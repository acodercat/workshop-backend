"""Validation for ``MessageCreate`` — a turn needs text, a file or answers.

The composer diverts a huge paste to a ``.txt`` attachment, leaving the
typed text empty; an attachments-only send is legal (the file is the
content). Only the truly-empty case (no text, no files) is rejected.
"""

import pytest
from pydantic import ValidationError

from app.schemas.session import AnsweredQuestion, FileRef, MessageCreate


def _file() -> FileRef:
    return FileRef(
        file_key="k",
        filename="pasted_text1.txt",
        size_bytes=1,
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


def test_timezone_is_optional_and_kept_as_sent() -> None:
    assert MessageCreate(content="hi").timezone is None
    assert MessageCreate(content="hi", timezone="Asia/Shanghai").timezone == "Asia/Shanghai"


@pytest.mark.parametrize("timezone", ["Mars/Olympus", "localtime", "+08:00", "", "x" * 200])
def test_a_timezone_the_deployment_cannot_compute_with_is_dropped(timezone: str) -> None:
    # The worker schedules a task in this zone, so one it could not resolve
    # is never stored on the turn — but the message itself still goes.
    assert MessageCreate(content="hi", timezone=timezone).timezone is None


def _answered(question: str, answer: list[str]) -> AnsweredQuestion:
    return AnsweredQuestion(question=question, answer=answer)


def test_answers_become_the_text_the_model_reads() -> None:
    msg = MessageCreate(
        answers=[
            _answered("Which format?", ["PDF", "Word"]),
            _answered("Add files?", []),
        ]
    )
    assert msg.content == "Which format?\n→ PDF, Word\n\nAdd files?\n→ (skipped)"


def test_answers_carry_no_text_of_their_own() -> None:
    with pytest.raises(ValidationError):
        MessageCreate(content="hi", answers=[_answered("Which format?", ["PDF"])])


def test_a_card_asks_at_most_four_questions() -> None:
    with pytest.raises(ValidationError):
        MessageCreate(answers=[_answered(f"Question {index}?", []) for index in range(5)])
