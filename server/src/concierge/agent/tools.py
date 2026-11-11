import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic_ai import Agent, RunContext

from concierge.agent import providers
from concierge.auth.models import User
from concierge.conversations import service as conversations
from concierge.core.db import get_sessionmaker
from concierge.documents.render import FORMATS, render_document
from concierge.media import service as media_service
from concierge.media.documents import extract_document
from concierge.memory import service as memory_service

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ToolDeps:
    """Deps injected into core tools during a run."""

    user_id: int
    conversation_id: int
    documents: list[dict[str, Any]] = field(default_factory=list)


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


DOCUMENT_INSTRUCTIONS = (
    "Напиши в Markdown финальное содержимое документа: "
    "заголовки через #, списки через -, таблицы — Markdown pipe-таблицами "
    "(csv и xlsx используют таблицы; несколько таблиц в xlsx станут "
    "несколькими листами, названными по ближайшему заголовку выше; в pptx "
    "каждый заголовок #/## начинает новый слайд). Воспринимай вход как "
    "бриф или черновик. Ответь только содержимым документа, без вступлений "
    "и без обрамления в ```."
)

_FENCE_RE = re.compile(r"^```[^\n]*\n(?P<body>.*)\n?```\s*$", re.DOTALL)


async def _write_content(fmt: str, title: str, content: str, user: User) -> str:
    """Optional `document` capability rewrites the draft into final content."""
    async with get_sessionmaker()() as session:
        bound = await providers.get_model(session, "document", user)
    if bound is None:
        return content
    try:
        agent = Agent(
            bound.model,
            instructions=DOCUMENT_INSTRUCTIONS,
            model_settings=bound.settings,
        )
        result = await agent.run(
            f"Название: {title}\nФормат файла: {fmt}\n\n{content}"  # noqa: RUF001
        )
        out = str(result.output).strip()
        m = _FENCE_RE.match(out)
        if m:
            out = m.group("body").strip()
        if out:
            return out
    except Exception:
        log.exception("document writer model failed")
    return content


async def create_document(
    ctx: RunContext[ToolDeps],
    format: Literal["md", "txt", "csv", "docx", "xlsx", "pptx", "pdf"],
    title: str,
    content: str,
) -> dict[str, Any]:
    """Create a file and attach it to the reply automatically.

    `content` is Markdown; tables are Markdown pipe tables (csv/xlsx use the
    tables; several tables in xlsx become several sheets named by the heading
    above each). In pptx every #/## heading starts a new slide. No need to
    paste the content or a link into the reply.
    """
    if format not in FORMATS:
        return {"error": f"unknown document format {format!r}"}
    async with get_sessionmaker()() as session:
        user = await session.get(User, ctx.deps.user_id)
    if user is None:
        return {"error": "user not found"}
    content = await _write_content(format, title, content, user)
    try:
        data, mime, name = await asyncio.to_thread(
            render_document, format, title, content
        )
        derived = await asyncio.to_thread(extract_document, mime, name, data)
    except ValueError as e:
        return {"error": str(e)}
    async with get_sessionmaker()() as session:
        att, _ = await media_service.create_attachment(
            session, user, name, mime, data, ctx.deps.conversation_id
        )
        att.derived = {
            "status": "done",
            "generated": True,
            **derived,
        }
        await session.commit()
        att_id, att_size = att.id, att.size
    part = {
        "type": "document",
        "attachment_id": att_id,
        "name": name,
        "mime": mime,
        "size": att_size,
        "status": "done",
        "generated": True,
    }
    ctx.deps.documents.append(part)
    return {
        "attachment_id": att_id,
        "name": name,
        "format": format,
        "size": att_size,
    }


CORE_TOOLS: dict[str, Callable[..., Any]] = {
    f.__name__: f
    for f in (
        search_messages,
        current_datetime,
        remember,
        recall,
        forget,
        create_document,
    )
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
