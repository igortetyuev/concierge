import asyncio
import ipaddress
import logging
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from pydantic_ai import Agent, RunContext

from concierge.agent import providers
from concierge.auth.models import User
from concierge.auth.permissions import has_permission
from concierge.channels import ChannelEvent, get_hub
from concierge.codeagent import service as codeagent_service
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
    run_id: int | None = None
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


FETCH_TEXT_MAX = 12000
_FETCH_BODY_MAX = 4 * 1024 * 1024
_FETCH_REDIRECTS = 5
_FETCH_TIMEOUT = 20.0
_DOC_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


async def _public_url_error(url: str) -> str | None:
    """Return an error string unless `url` is http(s) on a public host."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "only http(s) URLs are allowed"
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except (socket.gaierror, UnicodeError):
        return "cannot resolve host"
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            return "cannot resolve host"
        if not ip.is_global:
            return "host resolves to a non-public address"
    return None


def _html_to_text(html: str, url: str) -> str:
    text = trafilatura.extract(
        html, url=url, output_format="markdown", include_links=True
    )
    if text:
        return text
    plain = _TAG_RE.sub(" ", _SCRIPT_RE.sub(" ", html))
    return re.sub(r"[ \t]+", " ", unescape(plain))


async def _fetch_url(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    for _ in range(_FETCH_REDIRECTS + 1):
        if err := await _public_url_error(url):
            return {"error": err}
        try:
            async with client.stream(
                "GET", url, follow_redirects=False
            ) as resp:
                if resp.is_redirect and (loc := resp.headers.get("location")):
                    url = urljoin(str(resp.url), loc)
                    continue
                status = resp.status_code
                final_url = str(resp.url)
                ctype = (
                    resp.headers.get("content-type", "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                body = bytearray()
                truncated_body = False
                async for chunk in resp.aiter_bytes(65536):
                    body += chunk
                    if len(body) > _FETCH_BODY_MAX:
                        truncated_body = True
                        break
        except httpx.HTTPError as e:
            return {"error": f"request failed: {e}"}
        break
    else:
        return {"error": "too many redirects"}

    if status >= 400:
        return {"error": f"HTTP {status}", "url": final_url}
    data = bytes(body)
    if "html" in ctype or not ctype:
        text = await asyncio.to_thread(
            _html_to_text, data.decode("utf-8", "replace"), final_url
        )
    elif ctype.startswith("text/") or ctype in (
        "application/json",
        "application/xml",
        "application/javascript",
    ):
        text = data.decode("utf-8", "replace")
    elif ctype in _DOC_MIMES:
        name = urlparse(final_url).path.rsplit("/", 1)[-1] or "file"
        derived = await asyncio.to_thread(extract_document, ctype, name, data)
        text = str(derived.get("text") or "")
    else:
        return {"error": f"unsupported content type {ctype!r}"}
    return {
        "url": final_url,
        "status": status,
        "content_type": ctype,
        "text": text[:FETCH_TEXT_MAX],
        "truncated": len(text) > FETCH_TEXT_MAX or truncated_body,
    }


async def fetch_url(url: str) -> dict[str, Any]:
    """Fetch a web page (or document) by http(s) URL and return its text.

    HTML is reduced to readable Markdown; pdf/docx/xlsx/pptx and plain text
    are extracted directly. Use after web_search or for links from the user.
    """
    headers = {"User-Agent": "concierge-bot/1.0 (+family assistant)"}
    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT, headers=headers
    ) as client:
        return await _fetch_url(client, url)


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


async def delegate_coding_task(
    ctx: RunContext[ToolDeps], task: str, project: str
) -> dict[str, Any]:
    """Delegate a coding task to a CLI coding agent on the user's Mac.

    The agent (Devin/pi) works in an allowlisted project directory and its
    final report is returned. `project` is one of the configured project
    names; the task should be a self-contained brief.
    """
    hub = get_hub()
    last_sent = 0.0

    async def on_event(ev: Any) -> None:
        nonlocal last_sent
        now = time.monotonic()
        if last_sent and now - last_sent < 1.0:
            return
        last_sent = now
        await hub.publish(
            ChannelEvent(
                "message.tool",
                ctx.deps.conversation_id,
                {
                    "run_id": ctx.deps.run_id,
                    "type": "tool",
                    "name": "delegate_coding_task",
                    "tool_call_id": ctx.tool_call_id,
                    "status": "running",
                    "progress": ev.text[:200],
                },
            )
        )

    async with get_sessionmaker()() as session:
        user = await session.get(User, ctx.deps.user_id)
        if user is None:
            return {"error": "user not found"}
        return await codeagent_service.delegate(
            session, user, task, project, on_event
        )


CORE_TOOLS: dict[str, Callable[..., Any]] = {
    f.__name__: f
    for f in (
        search_messages,
        current_datetime,
        remember,
        recall,
        forget,
        create_document,
        delegate_coding_task,
        fetch_url,
    )
}

# Names that gate pydantic-ai capabilities rather than plain function tools;
# they share the tools.allow/deny lists in role permissions.
CAPABILITY_TOOLS = ("web_search",)


def allowed_tool_names(permissions: dict[str, Any]) -> list[str]:
    """Tool/capability names visible to a role under permissions.tools."""
    all_names = [*CORE_TOOLS, *CAPABILITY_TOOLS]
    if permissions.get("*"):
        names = list(all_names)
    else:
        conf = permissions.get("tools")
        if not isinstance(conf, dict):
            return []
        allow = conf.get("allow") or []
        deny = set(conf.get("deny") or [])
        if "*" in deny:
            return []
        names = [
            n
            for n in all_names
            if ("*" in allow or n in allow) and n not in deny
        ]
    if not has_permission(permissions, "code_agent"):
        names = [n for n in names if n != "delegate_coding_task"]
    return names


def allowed_capabilities(permissions: dict[str, Any]) -> list[Any]:
    """Pydantic-ai capabilities (native/local web tools) for the role."""
    from pydantic_ai.capabilities.web_search import WebSearch

    names = set(allowed_tool_names(permissions))
    caps: list[Any] = []
    if "web_search" in names:
        caps.append(WebSearch(native=True, local="duckduckgo"))
    return caps


def core_tools(permissions: dict[str, Any]) -> list[Callable[..., Any]]:
    return [
        CORE_TOOLS[n]
        for n in allowed_tool_names(permissions)
        if n in CORE_TOOLS
    ]
