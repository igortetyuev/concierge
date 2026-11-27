import asyncio
import dataclasses
import ipaddress
import logging
import re
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from html import unescape
from typing import Any, Literal
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import httpx
import trafilatura
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.agent import providers
from concierge.agent import service as agent_service
from concierge.agent.prompt import local_now
from concierge.auth.models import User
from concierge.auth.permissions import has_permission
from concierge.channels import ChannelEvent, get_hub
from concierge.codeagent import service as codeagent_service
from concierge.conversations import service as conversations
from concierge.conversations.models import Conversation
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
    # Seq/text of the message that triggered the run; tools that act on "the
    # messages before this request" use the seq as the cutoff (0 = no cutoff),
    # while conservative deterministic fallbacks may inspect the exact text.
    trigger_seq: int = 0
    trigger_text: str = ""
    documents: list[dict[str, Any]] = field(default_factory=list)
    # `choice` parts appended to the reply (ask_user_question).
    choices: list[dict[str, Any]] = field(default_factory=list)
    # `checklist` parts appended to the reply (show_checklist).
    checklists: list[dict[str, Any]] = field(default_factory=list)
    # Optional authoritative spoken rendering for the final text reply
    # (tools such as purchase_query use it for deterministic list speech).
    spoken_text: str | None = None
    # Name/id of the skill driving this run (pinned or ephemeral), if any;
    # interactive parts record them so their answers can re-apply it.
    skill_name: str | None = None
    skill_id: int | None = None
    # run_subagent recursion guard + per-run parallelism cap (5).
    subagent_depth: int = 0
    subagent_slots: asyncio.Semaphore | None = None
    # Per-run purchase mutation loop protection. These objects never touch the
    # DB and therefore reset naturally for the next user message/run.
    purchase_update_count: int = 0
    purchase_update_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    purchase_update_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
    """Return the current date and time in the family timezone (ISO 8601)."""
    return local_now().isoformat()


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


ASK_MODEL_ANSWER_MAX = 8000


def make_ask_model_tool(names: list[str]) -> Callable[..., Any]:
    """Build the ask_model tool with `model` restricted to `names`.

    Built per run: the enum in the tool schema mirrors enabled named_models.
    """
    model_enum = Literal[tuple(names)]  # type: ignore[valid-type]

    async def ask_model(
        ctx: RunContext[ToolDeps], model: str, question: str
    ) -> dict[str, Any]:
        """Ask a named external model a question and return its reply.

        Use when the user asks to consult a specific named model
        ("спроси Алису …"). The named model sees only `question` — a
        self-contained, single-shot prompt without chat history.
        """
        async with get_sessionmaker()() as session:
            user = await session.get(User, ctx.deps.user_id)
            named = await agent_service.get_named_model(session, model)
            if user is None:
                return {"error": "user not found"}
            if named is None:
                return {
                    "error": f"unknown or disabled model {model!r}",
                    "available": names,
                }
            bound = await providers.bind_named_model(session, user, named)
            system_prompt, display = named.system_prompt, named.name
            # The named model's tool checklist is additionally limited by
            # the caller's role permissions.
            caller_tools = set(allowed_tool_names(user.role.permissions))
            granted = [
                n
                for n in named.tools or []
                if n in NAMED_MODEL_TOOLS and n in caller_tools
            ]
        if bound is None:
            return {"error": f"model {display} is unavailable"}
        try:
            agent = Agent(
                bound.model,
                deps_type=ToolDeps,
                instructions=system_prompt,
                tools=[CORE_TOOLS[n] for n in granted if n in CORE_TOOLS],
                capabilities=capabilities_for(granted),
                model_settings=bound.settings,
            )
            result = await agent.run(question, deps=ctx.deps)
        except Exception as e:
            log.warning("ask_model(%s) failed: %s", display, e)
            return {"error": f"{display} failed: {e}"}
        return {
            "model": display,
            "answer": str(result.output)[:ASK_MODEL_ANSWER_MAX],
        }

    ask_model.__annotations__["model"] = model_enum
    return ask_model


FORWARD_MAX_MESSAGES = 20
FORWARD_SCAN_LIMIT = 50
FORWARD_NOTIFY_MAX = 200


async def _resolve_user(
    session: AsyncSession, name: str
) -> tuple[User | None, list[str]]:
    """Find one active user by display name / username / first name.

    Returns (user, []) on a unique hit, (None, candidates) when the name is
    ambiguous, (None, []) when nobody matches.
    """
    rows = (
        (
            await session.execute(
                select(User).where(User.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    needle = name.strip().lstrip("@").lower()
    if not needle:
        return None, []
    exact = [
        u
        for u in rows
        if u.display_name.lower() == needle or u.username.lower() == needle
    ]
    pool = exact or [
        u
        for u in rows
        if u.display_name.lower().startswith(needle)
        or u.username.lower().startswith(needle)
        or needle in u.display_name.lower().split()
    ]
    if len(pool) == 1:
        return pool[0], []
    return None, [u.display_name for u in pool]


async def _notify_dm(user_id: int, text: str) -> None:
    """Best-effort Telegram DM ping; no-op without a link or a live bot."""
    from concierge.channels.telegram import service as telegram_service

    try:
        await telegram_service.notify_user_dm(user_id, text)
    except Exception:
        log.debug("telegram dm notify failed user=%s", user_id)


async def invite_member(
    ctx: RunContext[ToolDeps], name: str
) -> dict[str, Any]:
    """Add a user to this conversation by name ("Позови Тихона").

    The invited user sees the session and its history right away. Works in
    personal sessions too — adding a member turns the session into a group.
    """
    from concierge.agent.runtime import _message_dict

    async with get_sessionmaker()() as session:
        conv = await session.get(Conversation, ctx.deps.conversation_id)
        requester = await session.get(User, ctx.deps.user_id)
        if conv is None or requester is None:
            return {"error": "conversation not found"}
        member_ids = {m.user_id for m in conv.members}
        if conv.status != "active" or requester.id not in member_ids:
            return {"error": "cannot invite into this conversation"}
        target, candidates = await _resolve_user(session, name)
        if target is None:
            return {
                "error": f"no unique user named {name!r}",
                "candidates": candidates,
            }
        if target.id in member_ids:
            return {
                "ok": True,
                "already_member": True,
                "user": target.display_name,
            }
        added = await conversations.add_members(session, conv, [target.id])
        if not added:
            return {"error": "failed to add member"}
        notice = await conversations.post_system_message(
            session,
            conv,
            [
                {
                    "type": "text",
                    "text": f"{target.display_name} добавлен в сессию",
                }
            ],
        )
        out = _message_dict(notice)
        conv_id, title = conv.id, conv.title
        target_id, display = target.id, target.display_name
        requester_name = requester.display_name
    hub = get_hub()
    updated = ChannelEvent(
        "conversation.updated", conv_id, {"conversation": {"id": conv_id}}
    )
    await hub.publish(updated)
    await hub.publish_users({target_id}, updated)
    created = ChannelEvent("message.created", conv_id, {"message": out})
    await hub.publish(created)
    await hub.publish_users({target_id}, created)
    await _notify_dm(
        target_id,
        f"{requester_name} пригласил вас в сессию «{title or conv_id}»",
    )
    return {"ok": True, "user": display, "conversation_id": conv_id}


async def forward_messages(
    ctx: RunContext[ToolDeps],
    recipient: str,
    count: int = 1,
    gap_minutes: float | None = None,
) -> dict[str, Any]:
    """Forward recent messages of this conversation to another user.

    "Перешли это Насте" -> count=1 sends the last message before this
    request; "Перешли последние сообщения" -> either pass the right count,
    or set gap_minutes to send the contiguous tail of messages separated by
    pauses of at most gap_minutes. The messages land in the direct session
    shared with the recipient (created on first use).
    """
    from concierge.agent.runtime import _message_dict

    count = max(1, min(int(count), FORWARD_MAX_MESSAGES))
    async with get_sessionmaker()() as session:
        conv = await session.get(Conversation, ctx.deps.conversation_id)
        requester = await session.get(User, ctx.deps.user_id)
        if conv is None or requester is None:
            return {"error": "conversation not found"}
        member_ids = {m.user_id for m in conv.members}
        if requester.id not in member_ids:
            return {"error": "not a member of this conversation"}
        target, candidates = await _resolve_user(session, recipient)
        if target is None:
            return {
                "error": f"no unique user named {recipient!r}",
                "candidates": candidates,
            }
        if target.id == requester.id:
            return {"error": "recipient is the requester"}
        if target.id in member_ids:
            return {
                "ok": True,
                "already_member": True,
                "user": target.display_name,
            }
        rows = await conversations.list_messages(
            session,
            conv.id,
            before_seq=ctx.deps.trigger_seq or None,
            limit=FORWARD_SCAN_LIMIT,
        )
        rows = [m for m in rows if m.author_kind in ("user", "assistant")]
        if gap_minutes is not None:
            gap = timedelta(minutes=max(gap_minutes, 0.0))
            selected = rows[-1:]
            for m in reversed(rows[:-1]):
                if (
                    m.created_at is None
                    or selected[0].created_at is None
                    or selected[0].created_at - m.created_at > gap
                ):
                    break
                selected.insert(0, m)
            selected = selected[-FORWARD_MAX_MESSAGES:]
        else:
            selected = rows[-count:]
        if not selected:
            return {"error": "nothing to forward"}
        target_conv, posted = await conversations.forward_to_user(
            session, requester, target, selected
        )
        delivered = [_message_dict(m) for m in posted]
        preview = message_text(selected[-1].parts).strip()[
            :FORWARD_NOTIFY_MAX
        ]
        requester_id, requester_name = requester.id, requester.display_name
        target_conv_id, target_id = target_conv.id, target.id
        display = target.display_name
    if not delivered:
        return {"error": "nothing forwardable in the selected messages"}
    hub = get_hub()
    await hub.publish_users(
        {requester_id, target_id},
        ChannelEvent(
            "conversation.updated",
            target_conv_id,
            {"conversation": {"id": target_conv_id}},
        ),
    )
    for out in delivered:
        await hub.publish_users(
            {requester_id, target_id},
            ChannelEvent(
                "message.created", target_conv_id, {"message": out}
            ),
        )
    n = len(delivered)
    suffix = preview if n == 1 else f"({n}) … {preview}"
    await _notify_dm(target_id, f"Переслано от {requester_name}: {suffix}")
    return {
        "ok": True,
        "user": display,
        "conversation_id": target_conv_id,
        "forwarded": n,
    }


QUESTION_MAX = 4
QUESTION_OPTIONS_MAX = 4


class QuestionOption(BaseModel):
    """One selectable option; the first option is the recommended one."""

    label: str
    description: str = ""


class AskQuestion(BaseModel):
    """A question with up to 4 options; empty options = open question."""

    question: str
    header: str = ""
    options: list[QuestionOption] = []
    multi_select: bool = False


async def ask_user_question(
    ctx: RunContext[ToolDeps], questions: list[AskQuestion]
) -> dict[str, Any]:
    """Ask the user a multiple-choice (or open) question.

    1-4 questions; each has `options` (up to 4; empty = open question,
    answered in free text) and `multi_select` for checkbox-style answers.
    A free-text "Другое" option always exists. The answer arrives as the
    next user message — finish your turn briefly after calling, don't guess.
    """
    if ctx.deps.choices:
        return {"error": "already asked a question this run"}
    if not questions or len(questions) > QUESTION_MAX:
        return {"error": "1-4 questions required"}
    for q in questions:
        if not q.question.strip():
            return {"error": "every question needs a non-empty text"}
        if len(q.options) > QUESTION_OPTIONS_MAX:
            return {"error": "at most 4 options per question"}
        for opt in q.options:
            if not opt.label.strip():
                return {"error": "every option needs a label"}
    ctx.deps.choices.append(
        {
            "type": "choice",
            "choice_id": uuid4().hex,
            "questions": [q.model_dump(mode="json") for q in questions],
            "answered": False,
            "skill": ctx.deps.skill_name,
            "skill_id": ctx.deps.skill_id,
        }
    )
    return {
        "ok": True,
        "note": (
            "вопрос показан пользователю; заверши ход коротко, не повторяя "
            "варианты — ответ придёт следующим сообщением"
        ),
    }


CHECKLIST_ITEMS_MAX = 50
CHECKLIST_COLUMNS_MAX = 6
CHECKLIST_ID_MAX = 64


class ChecklistItem(BaseModel):
    """One checklist row; `cells` are the table cells for table mode."""

    id: str = ""
    text: str
    cells: list[str] = []
    checked: bool = False


async def show_checklist(
    ctx: RunContext[ToolDeps],
    items: list[ChecklistItem],
    title: str = "",
    columns: list[str] | None = None,
    submit_label: str = "Отправить",
) -> dict[str, Any]:
    """Show the user an interactive checklist with checkboxes.

    1-50 items. List mode is the default; pass `columns` (up to 6) plus
    `cells` on every item for table mode — the two modes can't mix. `id`
    defaults to "1".."n". In table mode `text` is still required: it is the
    one-line summary used where tables can't render (Telegram, history,
    render_state). In the web app every tick is submitted automatically
    (no submit button); in Telegram the user presses `submit_label`.
    The checked state arrives as the next user message — finish your turn
    briefly after calling, don't list the items again.
    """
    if ctx.deps.checklists:
        return {"error": "already showed a checklist this run"}
    if not items or len(items) > CHECKLIST_ITEMS_MAX:
        return {"error": "1-50 items required"}
    cols = [str(c) for c in columns] if columns else []
    if len(cols) > CHECKLIST_COLUMNS_MAX:
        return {"error": "at most 6 columns"}
    seen: set[str] = set()
    out_items: list[dict[str, Any]] = []
    for i, it in enumerate(items):
        if not it.text.strip():
            return {"error": "every item needs a non-empty text"}
        iid = it.id.strip() or str(i + 1)
        if len(iid) > CHECKLIST_ID_MAX:
            return {"error": "item ids must be <= 64 chars"}
        if iid in seen:
            return {"error": "item ids must be unique"}
        seen.add(iid)
        if cols:
            if len(it.cells) != len(cols):
                return {
                    "error": "every item needs len(cells) == len(columns)"
                }
        elif it.cells:
            return {"error": "cells are only valid with columns"}
        out_items.append(
            {
                "id": iid,
                "text": it.text,
                "cells": list(it.cells) if cols else [],
                "checked": bool(it.checked),
            }
        )
    ctx.deps.checklists.append(
        {
            "type": "checklist",
            "checklist_id": uuid4().hex,
            "title": title,
            "columns": cols,
            "items": out_items,
            "submit_label": submit_label.strip() or "Отправить",
            "skill": ctx.deps.skill_name,
            "skill_id": ctx.deps.skill_id,
            "submissions": 0,
            "submitted_by": None,
            "submitted_at": None,
        }
    )
    return {
        "ok": True,
        "note": (
            "чек-лист показан пользователю; заверши ход коротко, не "
            "дублируя пункты — отмеченное состояние придёт следующим "
            "сообщением"
        ),
    }


SUBAGENT_TIMEOUT = 300.0
SUBAGENT_ANSWER_MAX = 8000
SUBAGENT_INSTRUCTIONS = (
    "Ты — субагент семейного консьержа. Выполни порученную подзадачу "
    "самостоятельно и верни только результат — кратко и по-русски. "
    "Диалоговой истории у тебя нет; вся необходимая информация — в задаче "  # noqa: RUF001
    "и контексте."
)
SUBAGENT_EXCLUDED = {
    "run_subagent",
    "ask_user_question",
    "show_checklist",
    "use_skill",
    "end_skill",
    "save_skill",
    "invite_member",
    "forward_messages",
    "schedule_task",
    "pause_scheduled_task",
    "resume_scheduled_task",
    "cancel_scheduled_task",
    "purchase_update",
    "purchase_share",
    "purchase_store",
}


async def run_subagent(
    ctx: RunContext[ToolDeps],
    goal: str,
    context: str = "",
    model: Literal["default", "fast"] = "default",
) -> dict[str, Any]:
    """Run a self-contained subtask in a fresh agent without chat history.

    Use for parallel/independent subtasks a skill asks for (Task/Agent in
    Claude terms, delegate_task in Hermes terms). `model="fast"` prefers the
    cheaper chat_fast binding. The subtask sees only `goal` + `context`.
    """
    if ctx.deps.subagent_depth >= 1:
        return {"error": "subagents cannot spawn subagents"}
    slots = ctx.deps.subagent_slots
    if slots is not None and slots.locked():
        return {"error": "subagent limit reached for this run"}

    async def _go() -> dict[str, Any]:
        async with get_sessionmaker()() as session:
            user = await session.get(User, ctx.deps.user_id)
            if user is None:
                return {"error": "user not found"}
            bound = None
            if model == "fast":
                bound = await providers.get_model(session, "chat_fast", user)
            bound = bound or await providers.get_model(session, "chat", user)
            if bound is None:
                return {"error": "no chat model bound"}
            perms = user.role.permissions
            funcs = [
                CORE_TOOLS[n]
                for n in allowed_tool_names(perms)
                if n in CORE_TOOLS and n not in SUBAGENT_EXCLUDED
            ]
            caps = capabilities_for(
                n
                for n in allowed_tool_names(perms)
                if n not in SUBAGENT_EXCLUDED
            )
        deps = dataclasses.replace(ctx.deps, subagent_depth=1)
        agent = Agent(
            bound.model,
            deps_type=ToolDeps,
            instructions=(
                f"{SUBAGENT_INSTRUCTIONS}\n\n[Сейчас] {local_now().isoformat()}"
            ),
            tools=funcs,
            capabilities=caps,
            model_settings=bound.settings,
        )
        prompt_text = goal if not context else f"{goal}\n\nКонтекст:\n{context}"  # noqa: RUF001
        try:
            result = await asyncio.wait_for(
                agent.run(prompt_text, deps=deps, usage=ctx.usage),
                SUBAGENT_TIMEOUT,
            )
        except TimeoutError:
            return {"error": "subagent timed out"}
        except Exception as e:
            log.warning("run_subagent failed: %s", e)
            return {"error": f"subagent failed: {e}"}
        return {"output": str(result.output)[:SUBAGENT_ANSWER_MAX]}

    if slots is None:
        return await _go()
    async with slots:
        return await _go()


def _schedule_tools() -> list[Callable[..., Any]]:
    from concierge.schedule import tools as schedule_tools

    return [
        schedule_tools.schedule_task,
        schedule_tools.list_scheduled_tasks,
        schedule_tools.pause_scheduled_task,
        schedule_tools.resume_scheduled_task,
        schedule_tools.cancel_scheduled_task,
    ]


def _purchase_tools() -> list[Callable[..., Any]]:
    from concierge.purchases import tools as purchase_tools

    return [
        purchase_tools.purchase_update,
        purchase_tools.purchase_query,
        purchase_tools.purchase_store,
        purchase_tools.purchase_share,
    ]


_CORE_TOOL_LIST: list[Callable[..., Any]] = [
    search_messages,
    current_datetime,
    remember,
    recall,
    forget,
    create_document,
    delegate_coding_task,
    fetch_url,
    invite_member,
    forward_messages,
    *_schedule_tools(),
    *_purchase_tools(),
]

CORE_TOOLS: dict[str, Callable[..., Any]] = {
    f.__name__: f for f in _CORE_TOOL_LIST
}

# Names that gate pydantic-ai capabilities rather than plain function tools;
# they share the tools.allow/deny lists in role permissions.
CAPABILITY_TOOLS = ("web_search",)

# Tools built per run from DB state rather than static functions; they share
# the tools.allow/deny lists too.
DYNAMIC_TOOLS = (
    "ask_model",
    "use_skill",
    "end_skill",
    "read_skill_file",
    "save_skill",
    "ask_user_question",
    "show_checklist",
    "run_subagent",
)

# Tool names selectable per named model (its `tools` checklist); ask_model
# itself is excluded — no recursion.
NAMED_MODEL_TOOLS = tuple(CORE_TOOLS) + CAPABILITY_TOOLS


def allowed_tool_names(permissions: dict[str, Any]) -> list[str]:
    """Tool/capability names visible to a role under permissions.tools."""
    all_names = [*CORE_TOOLS, *CAPABILITY_TOOLS, *DYNAMIC_TOOLS]
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
    if not has_permission(permissions, "create_skills"):
        names = [n for n in names if n != "save_skill"]
    return names


def capabilities_for(names: Iterable[str]) -> list[Any]:
    """Pydantic-ai capabilities (native/local web tools) for tool names."""
    from pydantic_ai.capabilities.web_search import WebSearch

    caps: list[Any] = []
    if "web_search" in names:
        caps.append(WebSearch(native=True, local="duckduckgo"))
    return caps


def allowed_capabilities(permissions: dict[str, Any]) -> list[Any]:
    """Pydantic-ai capabilities (native/local web tools) for the role."""
    return capabilities_for(allowed_tool_names(permissions))


def core_tools(permissions: dict[str, Any]) -> list[Callable[..., Any]]:
    return [
        CORE_TOOLS[n]
        for n in allowed_tool_names(permissions)
        if n in CORE_TOOLS
    ]
