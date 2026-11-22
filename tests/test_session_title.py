"""A session's LLM-written title: what is taken from the model's output, and
when a completion is no title at all.

No network — the client is replaced where ``generate_title`` is driven.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai import OpenAIError

from app.services.session import title as title_module
from app.services.session.title import generate_title, title_from_output


def test_a_plain_title_is_kept_as_written():
    assert title_from_output("季度销售报告整理") == "季度销售报告整理"


def test_only_the_first_non_empty_line_is_the_title():
    output = "\n\nComparing database engines\n\nThis title reflects the request."

    assert title_from_output(output) == "Comparing database engines"


@pytest.mark.parametrize(
    "output",
    [
        '"Fix the login bug"',
        "「Fix the login bug」",
        "**Fix the login bug**",
        "*Fix the login bug*",
    ],
)
def test_quotes_and_emphasis_around_the_title_are_dropped(output):
    assert title_from_output(output) == "Fix the login bug"


def test_emphasis_inside_the_title_is_not_wrapping():
    assert title_from_output("*a* and *b*") == "*a* and *b*"


def test_control_and_invisible_characters_are_dropped():
    output = "Weekly\N{ZERO WIDTH SPACE} \N{RIGHT-TO-LEFT OVERRIDE}report\N{ESCAPE}"

    assert title_from_output(output) == "Weekly report"


def test_a_long_title_is_cut_to_length():
    assert title_from_output("字" * 50) == "字" * 30


def test_output_with_no_text_gives_no_title():
    assert title_from_output("") == ""
    assert title_from_output(" \n\t\n") == ""
    assert title_from_output('""') == ""


def _completion(content, finish_reason="stop"):
    message = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])


@pytest.fixture
def create(monkeypatch):
    """The client's ``chat.completions.create``, to answer as a test says."""
    create = AsyncMock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(title_module, "_client", client)
    return create


async def test_the_title_is_taken_from_the_completion(create):
    create.return_value = _completion("**整理周报**\n以上是标题。")

    assert await generate_title("帮我整理一下这周的周报") == "整理周报"


async def test_a_completion_cut_off_at_the_output_limit_is_not_a_title(create):
    create.return_value = _completion("Comparing data", finish_reason="length")

    with pytest.raises(OpenAIError):
        await generate_title("Compare three database engines")


async def test_a_completion_with_no_text_is_not_a_title(create):
    create.return_value = _completion(None)

    with pytest.raises(OpenAIError):
        await generate_title("hello")
