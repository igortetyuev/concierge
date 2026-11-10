from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic_ai import RunContext

from concierge.auth.models import User
from concierge.conversations import service as conversations
from concierge.core.db import get_sessionmaker
from concierge.memory import service as memory_service


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


async def remember(
    ctx: RunContext[ToolDeps], text: str, scope: str = "user"
) -> dict[str, Any]:
    """Save a fact to long-term memory.

    scope: "user" — personal to the requester; "family" — shared with the
    whole family (requires the family_memory permission).
    """
    if scope not in ("user", "family"):
        scope = "user"
    async with get_sessionmaker()() as session:
        user = await session.get(User, ctx.deps.user_id)
        if user is None:
            return {"error": "user not found"}
        try:
            item = await memory_service.remember(
                session, user, text.strip(), scope
            )
        except PermissionError:
            return {"error": "no permission to write family memory"}
    return {"id": item.id, "scope": item.scope}


async def recall(
    ctx: RunContext[ToolDeps], query: str, limit: int = 5
) -> list[dict[str, Any]]:
    """Search long-term memory (personal + family) for facts about `query`."""
    async with get_sessionmaker()() as session:
        user = await session.get(User, ctx.deps.user_id)
        if user is None:
            return []
        return await memory_service.recall(session, user, query, limit)


async def forget(ctx: RunContext[ToolDeps], memory_id: int) -> dict[str, Any]:
    """Delete a memory item by its id (as returned by `recall`)."""
    async with get_sessionmaker()() as session:
        user = await session.get(User, ctx.deps.user_id)
        if user is None:
            return {"ok": False, "error": "user not found"}
        try:
            ok = await memory_service.forget(session, user, int(memory_id))
        except PermissionError:
            return {"ok": False, "error": "not allowed"}
    return {"ok": ok}


def message_text(parts: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(p.get("text", "")) for p in parts if p.get("type") == "text"
    )


CORE_TOOLS: dict[str, Callable[..., Any]] = {
    f.__name__: f
    for f in (search_messages, current_datetime, remember, recall, forget)
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
