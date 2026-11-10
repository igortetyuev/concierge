from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.conversations.models import Conversation
from concierge.settings import service as settings_service

DEFAULT_ASSISTANT_NAME = "Консьерж"

DEFAULT_SYSTEM_PROMPT = (
    "Ты — {name}, семейный агент-консьерж. Отвечай по-русски, кратко и по "
    "существу, если не просят иного. Учитывай роль и возраст собеседника. "
    "Групповые сессии: отвечай только на прямые обращения к тебе."
)

# Mention tokens that trigger a reply in reply_mode="mention" conversations.
BASE_MENTIONS = ("@консьерж", "@assistant", "@concierge")


async def assistant_name(session: AsyncSession, user: User) -> str:
    value = await settings_service.get_value(
        session, user, "agent.name", DEFAULT_ASSISTANT_NAME
    )
    return str(value) if value else DEFAULT_ASSISTANT_NAME


async def system_prompt(
    session: AsyncSession,
    user: User,
    conv: Conversation,
    member_names: list[str],
    memory_block: str | None = None,
) -> str:
    """Compose the system prompt once per run: base + role + user + context."""
    base = await settings_service.get_value(
        session, user, "agent.system_prompt", DEFAULT_SYSTEM_PROMPT
    )
    name = await assistant_name(session, user)
    chunks = [str(base).replace("{name}", name)]

    if user.role.prompt_fragment:
        chunks.append(f"[Роль: {user.role.name}]\n{user.role.prompt_fragment}")
    if user.prompt_fragment:
        chunks.append(
            f"[Пользователь: {user.display_name}]\n{user.prompt_fragment}"
        )

    kind = "групповая" if conv.kind == "group" else "личная"
    ctx = f"[Сессия] вид: {kind}"
    if conv.title:
        ctx += f", название: {conv.title}"
    if len(member_names) > 1:
        ctx += ", участники: " + ", ".join(member_names)
    chunks.append(ctx)

    if memory_block:
        chunks.append(f"[Память]\n{memory_block}")

    return "\n\n".join(chunks)


async def mentions(session: AsyncSession, user: User) -> tuple[str, ...]:
    name = await assistant_name(session, user)
    tokens = {t.lower() for t in BASE_MENTIONS}
    tokens.add(f"@{name}".lower())
    return tuple(tokens)


def is_mentioned(parts: list[dict[str, Any]], tokens: tuple[str, ...]) -> bool:
    text = " ".join(
        str(p.get("text", "")) for p in parts if p.get("type") == "text"
    ).lower()
    return any(t in text for t in tokens)
