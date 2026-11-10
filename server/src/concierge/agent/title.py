import logging

from pydantic_ai import Agent

from concierge.agent import providers
from concierge.auth.models import User
from concierge.conversations.models import Conversation
from concierge.core.db import get_sessionmaker

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Придумай короткий заголовок (до 6 слов) для чат-сессии по первому "
    "сообщению пользователя. Ответь только заголовком, без кавычек и точки "
    "в конце."
)

TITLE_MAX_LEN = 128
FALLBACK_LEN = 64


def _clean(text: str, limit: int) -> str:
    line = text.strip().strip('"«»\'').split("\n", 1)[0].strip()
    return line[:limit]


async def generate(
    conversation_id: int, user: User, first_text: str
) -> str | None:
    """Set the conversation title via capability chat_fast; truncated-text
    fallback. Returns the new title, or None if it was already set/empty."""
    if not first_text.strip():
        return None
    async with get_sessionmaker()() as session:
        conv = await session.get(Conversation, conversation_id)
        if conv is None or conv.title:
            return None
        bound = await providers.get_model(session, "chat_fast", user)
        new_title = ""
        if bound is not None:
            try:
                agent = Agent(
                    bound.model,
                    instructions=INSTRUCTIONS,
                    model_settings=bound.settings,
                )
                result = await agent.run(first_text)
                new_title = _clean(result.output, TITLE_MAX_LEN)
            except Exception:
                log.exception("title generation failed conv=%s", conv.id)
        conv.title = new_title or _clean(first_text, FALLBACK_LEN)
        await session.commit()
        return conv.title
