from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic_ai import RunContext

from concierge.conversations import service as conversations
from concierge.core.db import get_sessionmaker


@dataclass(slots=True)
class ToolDeps:
    """Deps injected into core tools during a run."""

    user_id: int
    conversation_id: int


async def search_messages(
    ctx: RunContext[ToolDeps], query: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Search the current user's conversations for messages containing a query.

    Returns matching messages with conversation id, seq, author and text.
    """
    async with get_sessionmaker()() as session:
        rows = await conversations.search_messages(
            session, ctx.deps.user_id, query, limit
        )
    return [
        {
            "conversation_id": m.conversation_id,
            "seq": m.seq,
            "author_kind": m.author_kind,
            "text": message_text(m.parts),
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in rows
    ]


def current_datetime() -> str:
    """Return the current date and time (UTC, ISO 8601)."""
    return datetime.now(UTC).isoformat()


def message_text(parts: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(p.get("text", "")) for p in parts if p.get("type") == "text"
    )


CORE_TOOLS: dict[str, Callable[..., Any]] = {
    f.__name__: f for f in (search_messages, current_datetime)
}


def allowed_tool_names(permissions: dict[str, Any]) -> list[str]:
    """Core tool names visible to a role under permissions.tools allow/deny."""
    if permissions.get("*"):
        return list(CORE_TOOLS)
    conf = permissions.get("tools")
    if not isinstance(conf, dict):
        return []
    allow = conf.get("allow") or []
    deny = set(conf.get("deny") or [])
    if "*" in deny:
        return []
    return [
        n
        for n in CORE_TOOLS
        if ("*" in allow or n in allow) and n not in deny
    ]


def core_tools(permissions: dict[str, Any]) -> list[Callable[..., Any]]:
    return [CORE_TOOLS[n] for n in allowed_tool_names(permissions)]
