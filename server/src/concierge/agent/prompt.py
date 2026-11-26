from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.conversations.models import Conversation
from concierge.core.config import get_settings
from concierge.settings import service as settings_service

DEFAULT_ASSISTANT_NAME = "Консьерж"

_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def local_now() -> datetime:
    """Current time in the configured family timezone."""
    return datetime.now(ZoneInfo(get_settings().timezone))

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
    skills_catalog: str | None = None,
    active_skill: str | None = None,
) -> str:
    """Compose the system prompt once per run: base + role + user + context."""
    base = await settings_service.get_value(
        session, user, "agent.system_prompt", DEFAULT_SYSTEM_PROMPT
    )
    name = await assistant_name(session, user)
    chunks = [str(base).replace("{name}", name)]

    if user.role.prompt_fragment:
        chunks.append(f"[Роль: {user.role.name}]\n{user.role.prompt_fragment}")
    display_name = (user.display_name or "").strip() or user.username
    user_chunk = f"[Пользователь: {display_name}]"
    if user.prompt_fragment:
        user_chunk += f"\n{user.prompt_fragment}"
    chunks.append(user_chunk)

    kind = "групповая" if conv.kind == "group" else "личная"
    ctx = f"[Сессия] вид: {kind}"
    if conv.title:
        ctx += f", название: {conv.title}"
    if len(member_names) > 1:
        ctx += ", участники: " + ", ".join(member_names)
    chunks.append(ctx)

    if conv.origin == "voice_quick" or (conv.settings or {}).get(
        "voice_session"
    ):
        chunks.append(
            "[Голосовая сессия] Ответ будет озвучен — отвечай кратко и "
            "разговорно. Если информации много, давай её полностью в тексте: "
            "вслух прозвучит автоматически сжатая версия."
        )

    now = local_now()
    off = now.strftime("%z")
    chunks.append(
        f"[Сейчас] {_WEEKDAYS[now.weekday()]}, "
        f"{now:%d.%m.%Y %H:%M} ({now:%Z}, UTC{off[:3]}:{off[3:]})"
    )

    if memory_block:
        chunks.append(f"[Память]\n{memory_block}")

    if skills_catalog:
        chunks.append(
            f"[Скиллы]\n{skills_catalog}\n"
            "Запуск: инструмент `use_skill` или /команда от пользователя."
        )

    if active_skill:
        chunks.append(active_skill)

    return "\n\n".join(chunks)


async def mentions(session: AsyncSession, user: User) -> tuple[str, ...]:
    name = await assistant_name(session, user)
    tokens = {t.lower() for t in BASE_MENTIONS}
    tokens.add(f"@{name}".lower())
    return tuple(tokens)


def is_mentioned(parts: list[dict[str, Any]], tokens: tuple[str, ...]) -> bool:
    # A mention part counts only when it targets the assistant: web clients
    # tag people mentions with user_id; the bare {"type": "mention"} marker
    # (telegram, or assistant=true) means the concierge.
    if any(
        p.get("type") == "mention" and p.get("user_id") is None for p in parts
    ):
        return True
    text = " ".join(
        str(p.get("text", "")) for p in parts if p.get("type") == "text"
    ).lower()
    if any(t in text for t in tokens):
        return True
    # Voice transcripts have no "@": match the bare trigger words there.
    spoken = " ".join(
        str(p.get("text", ""))
        for p in parts
        if p.get("type") == "text" and p.get("transcribed")
    ).lower()
    return any(t.lstrip("@") in spoken for t in tokens)
