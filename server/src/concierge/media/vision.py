"""Image description through the `vision` capability binding.

Uses the bound chat model via PydanticAI with BinaryContent parts — works for
any vision-capable provider (openai, anthropic, google, openai-compatible);
the `test` provider returns its canned output for tests.
"""

import logging

from pydantic_ai import Agent
from pydantic_ai.messages import BinaryContent
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User

log = logging.getLogger(__name__)

_IMAGE_PROMPT = (
    "Опиши изображение кратко и по делу на русском: что на нём, важный "
    "текст/надписи, кто и что делает. Две-три фразы. Если на изображении "
    "есть связный текст (задача, документ, переписка) — перепиши текст "
    "дословно и полностью, включая числа, формулы и обозначения."
)
_VIDEO_PROMPT = (
    "Это кадры из видео. Опиши кратко на русском, что происходит в видео и "
    "какой важный текст виден. Две-три фразы."
)


async def describe_images(
    session: AsyncSession,
    user: User,
    images: list[tuple[bytes, str]],
    *,
    video: bool = False,
) -> str | None:
    """Caption image/video frames; None when no vision model is bound."""
    if not images:
        return None
    # Lazy: concierge.agent imports the media pipeline at module load.
    from concierge.agent import providers

    bound = await providers.get_model(session, "vision", user)
    if bound is None:
        return None
    content = [_VIDEO_PROMPT if video else _IMAGE_PROMPT] + [
        BinaryContent(data=data, media_type=mime) for data, mime in images
    ]
    try:
        agent = Agent(bound.model, model_settings=bound.settings)
        result = await agent.run(content)
        return str(result.output).strip() or None
    except Exception:
        log.exception("vision describe failed user=%s", user.id)
        return None
