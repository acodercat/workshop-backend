"""LLM-backed session-title generation.

A single stateless helper: take a user message, return a short
Chinese title describing what the conversation is about. The backend
calls it from the ``POST /titles`` endpoint; there's no DB persistence
or session awareness here — the caller decides whether to save it.

One shared ``AsyncOpenAI`` client is created per process at import
time; it reads settings from ``config.settings`` (``llm_base_url``,
``llm_api_key``, ``llm_model``). No streaming and no agent loop — a
single short completion.
"""

import logging

from openai import AsyncOpenAI, OpenAIError

from config import settings

logger = logging.getLogger(__name__)

# A defensive cap so a runaway tool call with a giant prompt doesn't
# blow out the generator. 4k chars is plenty for any reasonable first
# message — we just want a topic summary, not the full text.
_MAX_CONTENT_CHARS = 4000
_TITLE_MAX_CHARS = 30

_SYSTEM_PROMPT = (
    "你是一个对话标题生成器。阅读用户给出的首条消息，"
    "用中文生成一个 15 字以内、简洁、能体现主题的标题。"
    "只返回标题本身，不要引号、不要标点、不要解释。"
)

_client = AsyncOpenAI(
    base_url=settings.llm_base_url,
    api_key=settings.llm_api_key,
)


async def generate_title(content: str) -> str:
    """Return a short Chinese title summarizing ``content``.

    Raises :class:`openai.OpenAIError` on network/upstream failure —
    the endpoint translates that into a 502 so the frontend can fall
    back to its own truncated placeholder.
    """
    trimmed = content[:_MAX_CONTENT_CHARS]
    response = await _client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": trimmed},
        ],
        max_tokens=40,
        temperature=0.3,
    )
    raw = (response.choices[0].message.content or "").strip()
    # Defensive: strip quotes the model sometimes adds despite the
    # system prompt, and cap length so one rogue completion can't
    # push a 200-char "title" into the UI.
    cleaned = raw.strip("「」\"'`“”‘’ ")
    if not cleaned:
        raise OpenAIError("title generation returned empty content")
    return cleaned[:_TITLE_MAX_CHARS]
