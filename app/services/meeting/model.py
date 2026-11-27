"""The system model, for the meeting features that use it.

One completion shape for all of them: a system instruction, and the
material as a JSON document in the user turn — so a transcript is never
mistaken for instructions, which every prompt also says.
"""

import json
import logging
from functools import cache
from typing import Any

from openai import AsyncOpenAI, OpenAIError

from app.errors import MeetingModelUnavailableError
from config import settings

logger = logging.getLogger(__name__)


@cache
def _client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.meeting_llm_base_url or settings.llm_base_url,
        api_key=settings.meeting_llm_api_key or settings.llm_api_key,
        timeout=settings.meeting_llm_timeout_seconds,
        # A completion either finishes inside the timeout or is not worth
        # waiting for twice: the user is watching a spinner.
        max_retries=0,
    )


async def complete(model: str, *, instructions: str, material: dict[str, Any]) -> str:
    """Run one completion over ``material`` with ``model`` (the deployment's
    choice, :func:`app.services.meeting.settings.read`). Only a finished
    answer counts: one cut short by length would be presented as whole
    minutes."""
    try:
        response = await _client().chat.completions.create(
            model=model,
            temperature=0.2,
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(material, ensure_ascii=False)},
            ],
        )
    except OpenAIError as exc:
        logger.warning("meeting model %s failed: %s", model, type(exc).__name__)
        raise MeetingModelUnavailableError("The model did not answer.") from exc
    choice = response.choices[0] if response.choices else None
    content = (choice.message.content or "").strip() if choice else ""
    if choice is None or choice.finish_reason != "stop" or not content:
        raise MeetingModelUnavailableError("The model's answer was incomplete.")
    return content
