import asyncio
import dataclasses
import logging
from typing import Any

from pydantic_ai import Agent, AgentRunResultEvent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    UserPromptPart,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.agent import mcp, prompt, providers, title, tools
from concierge.agent import service as agent_service
from concierge.agent.tools import ToolDeps, message_text
from concierge.auth.models import User
from concierge.channels import ChannelEvent, Hub, get_hub
from concierge.conversations import service as conversations
from concierge.conversations.models import (
    Conversation,
    ConversationMember,
    Message,
    Run,
)
from concierge.core.db import get_sessionmaker, utcnow
from concierge.media import service as media_service
from concierge.media import tts
from concierge.media.models import Attachment
from concierge.media.pipeline import get_pipeline
from concierge.memory import indexer as memory_indexer
from concierge.memory import service as memory_service
from concierge.settings import service as settings_service

log = logging.getLogger(__name__)

HISTORY_LIMIT = 100
MEDIA_TEXT_MAX = 4000
TTS_TEXT_MAX = 4000

ERROR_TEXT = "Произошла ошибка при обработке запроса."


class AgentRuntime:
    """Runs the PydanticAI agent for a trigger message; one run per session."""

    def __init__(self, hub: Hub) -> None:
        self._hub = hub
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(self, conversation_id: int, trigger_message_id: int) -> None:
        task = asyncio.create_task(
            self.maybe_run(conversation_id, trigger_message_id)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """Await all currently scheduled runs (tests, graceful shutdown)."""
        while self._tasks:
            await asyncio.gather(*self._tasks)

    async def maybe_run(
        self, conversation_id: int, trigger_message_id: int
    ) -> None:
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        if lock.locked():
            return  # one run per conversation at a time
        async with lock:
            try:
                await self._run(conversation_id, trigger_message_id)
            except Exception:
                log.exception("agent run failed conv=%s", conversation_id)

    async def _run(self, conversation_id: int, trigger_message_id: int) -> None:
        session_maker = get_sessionmaker()
        async with session_maker() as session:
            conv = await session.get(Conversation, conversation_id)
            trigger = await session.get(Message, trigger_message_id)
            if conv is None or trigger is None or trigger.author_id is None:
                return
            user = await session.get(User, trigger.author_id)
            if user is None or conv.status != "active":
                return
            if conv.reply_mode == "mention" and not prompt.is_mentioned(
                trigger.parts, await prompt.mentions(session, user)
            ):
                return

            member_names = await self._member_names(session, conv.id)
            bound = await providers.get_model(session, "chat", user)
            # STT/vision for the trigger's attachments must land before the
            # run reads them; history rows are resolved as they stand.
            trigger_att = {
                aid
                for p in trigger.parts
                if (aid := media_service.part_attachment_id(p)) is not None
            }
            if trigger_att:
                await get_pipeline().wait_for(trigger_att)
            history_rows = await conversations.list_messages(
                session, conv.id, before_seq=trigger.seq, limit=HISTORY_LIMIT
            )
            atts = await self._attachments(session, [*history_rows, trigger])
            trigger_text = self._content(
                trigger.parts, conv, member_names, user.id, atts
            )
            memory_block = await memory_service.retrieve_context(
                session, user, conv, trigger_text
            )
            instructions = await prompt.system_prompt(
                session, user, conv, list(member_names.values()), memory_block
            )
            toolsets = await mcp.build_toolsets(session, user)
            funcs = tools.core_tools(user.role.permissions)
            if "ask_model" in tools.allowed_tool_names(
                user.role.permissions
            ) and (
                named_names := await agent_service.enabled_named_model_names(
                    session
                )
            ):
                funcs.append(tools.make_ask_model_tool(named_names))
            capabilities = tools.allowed_capabilities(user.role.permissions)
            history = self._build_history(
                history_rows, conv, member_names, atts
            )
            deps = ToolDeps(user_id=user.id, conversation_id=conv.id)

            run = Run(
                conversation_id=conv.id,
                trigger_message_id=trigger.id,
                status="running",
                model=bound.label if bound else None,
                started_at=utcnow(),
            )
            session.add(run)
            await session.commit()
            run_id = run.id
            deps.run_id = run_id
            await self._run_status(conv.id, run_id, "running", model=run.model)

        if bound is None:
            await self._finish(
                run_id, conv.id, "failed", error="no chat model bound"
            )
            return

        agent = Agent(
            bound.model,
            deps_type=ToolDeps,
            instructions=instructions,
            tools=funcs,
            toolsets=toolsets,
            capabilities=capabilities,
            model_settings=bound.settings,
        )
        tool_parts: list[dict[str, Any]] = []
        output = ""
        usage: dict[str, Any] = {}
        try:
            async with agent, agent.run_stream_events(
                trigger_text,
                message_history=history,
                deps=deps,
            ) as stream:
                async for event in stream:
                    # First chunk of a text part arrives as PartStartEvent,
                    # subsequent chunks as PartDeltaEvent(TextPartDelta).
                    if (
                        isinstance(event, PartDeltaEvent)
                        and isinstance(event.delta, TextPartDelta)
                    ):
                        delta_text = event.delta.content_delta
                    elif (
                        isinstance(event, PartStartEvent)
                        and isinstance(event.part, TextPart)
                    ):
                        delta_text = event.part.content
                    else:
                        delta_text = ""
                    if delta_text:
                        await self._emit(
                            "message.delta",
                            conv.id,
                            {"run_id": run_id, "text": delta_text},
                        )
                    if isinstance(event, FunctionToolCallEvent):
                        part = {
                            "type": "tool",
                            "name": event.part.tool_name,
                            "tool_call_id": event.part.tool_call_id,
                            "status": "running",
                        }
                        tool_parts.append(part)
                        await self._emit(
                            "message.tool", conv.id, {"run_id": run_id, **part}
                        )
                    elif (
                        isinstance(event, PartStartEvent)
                        and isinstance(event.part, NativeToolCallPart)
                    ):
                        # Provider-side tools (e.g. web_search) arrive as part
                        # events, not FunctionToolCallEvent.
                        part = {
                            "type": "tool",
                            "name": event.part.tool_name,
                            "tool_call_id": event.part.tool_call_id or "",
                            "status": "running",
                        }
                        tool_parts.append(part)
                        await self._emit(
                            "message.tool", conv.id, {"run_id": run_id, **part}
                        )
                    elif (
                        isinstance(event, PartStartEvent)
                        and isinstance(event.part, NativeToolReturnPart)
                    ):
                        for p in tool_parts:
                            if p["tool_call_id"] == event.part.tool_call_id:
                                p["status"] = "done"
                        await self._emit(
                            "message.tool",
                            conv.id,
                            {
                                "run_id": run_id,
                                "type": "tool",
                                "name": event.part.tool_name,
                                "tool_call_id": event.part.tool_call_id,
                                "status": "done",
                            },
                        )
                    elif isinstance(event, FunctionToolResultEvent):
                        status = (
                            "error"
                            if isinstance(event.part, RetryPromptPart)
                            else "done"
                        )
                        for p in tool_parts:
                            if p["tool_call_id"] == event.part.tool_call_id:
                                p["status"] = status
                        await self._emit(
                            "message.tool",
                            conv.id,
                            {
                                "run_id": run_id,
                                "type": "tool",
                                "name": event.part.tool_name,
                                "tool_call_id": event.part.tool_call_id,
                                "status": status,
                            },
                        )
                    elif isinstance(event, AgentRunResultEvent):
                        output = str(event.result.output)
                        usage = dataclasses.asdict(event.result.usage)
        except Exception as exc:
            log.exception("run %s failed", run_id)
            await self._finish(run_id, conv.id, "failed", error=str(exc))
            await self._save_reply(
                session_maker,
                conv.id,
                run_id,
                ERROR_TEXT,
                tool_parts,
                deps.documents,
            )
            return

        await self._finish(run_id, conv.id, "done", usage=usage)
        await self._save_reply(
            session_maker, conv.id, run_id, output, tool_parts, deps.documents
        )
        await self._ensure_title(conv.id, user, trigger.parts)
        await self._maybe_tts_reply(conv.id, user, output)

    async def _member_names(
        self, session: AsyncSession, conversation_id: int
    ) -> dict[int, str]:
        rows = (
            await session.execute(
                select(ConversationMember.user_id, User.display_name)
                .join(User, User.id == ConversationMember.user_id)
                .where(ConversationMember.conversation_id == conversation_id)
            )
        ).all()
        return {uid: name for uid, name in rows}

    async def _attachments(
        self, session: AsyncSession, messages: list[Message]
    ) -> dict[int, Attachment]:
        ids = {
            aid
            for m in messages
            for p in m.parts
            if (aid := media_service.part_attachment_id(p)) is not None
        }
        if not ids:
            return {}
        rows = (
            await session.execute(
                select(Attachment).where(Attachment.id.in_(ids))
            )
        ).scalars()
        return {a.id: a for a in rows}

    def _build_history(
        self,
        rows: list[Message],
        conv: Conversation,
        member_names: dict[int, str],
        atts: dict[int, Attachment],
    ) -> list[ModelMessage]:
        out: list[ModelMessage] = []
        for m in rows:
            text = self._content(m.parts, conv, member_names, m.author_id, atts)
            if not text:
                continue
            if m.author_kind == "assistant":
                out.append(ModelResponse(parts=[TextPart(text)]))
            else:
                out.append(ModelRequest(parts=[UserPromptPart(text)]))
        return out

    @staticmethod
    def _content(
        parts: list[dict[str, Any]],
        conv: Conversation,
        member_names: dict[int, str],
        author_id: int | None,
        atts: dict[int, Attachment],
    ) -> str:
        chunks = []
        for p in parts:
            ptype = p.get("type")
            if ptype == "text":
                chunks.append(str(p.get("text", "")))
            elif ptype in ("tool", "mention"):
                continue
            elif (aid := media_service.part_attachment_id(p)) is not None and (
                att := atts.get(aid)
            ) is not None:
                label = att.original_name or ptype or "attachment"
                text = media_service.media_prompt_text(att, str(ptype))
                if ptype in ("audio", "video"):
                    chunks.append(text or f"[{label}]")
                elif text:
                    chunks.append(f"[{label}]\n{text[:MEDIA_TEXT_MAX]}")
                else:
                    chunks.append(f"[{label}]")
            else:
                chunks.append(f"[{ptype or 'attachment'}]")
        text = "\n".join(c for c in chunks if c)
        if conv.kind == "group" and author_id is not None:
            text = f"{member_names.get(author_id, 'unknown')}: {text}"
        return text

    async def _emit(
        self, event_type: str, conversation_id: int, data: dict[str, Any]
    ) -> None:
        await self._hub.publish(ChannelEvent(event_type, conversation_id, data))

    async def _run_status(
        self, conversation_id: int, run_id: int, status: str, **extra: Any
    ) -> None:
        await self._emit(
            "run.status",
            conversation_id,
            {"run_id": run_id, "status": status, **extra},
        )

    async def _finish(
        self,
        run_id: int,
        conversation_id: int,
        status: str,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        async with get_sessionmaker()() as session:
            run = await session.get(Run, run_id)
            if run is not None:
                run.status = status
                run.finished_at = utcnow()
                if usage is not None:
                    run.usage = usage
                run.error = error
                await session.commit()
        await self._run_status(conversation_id, run_id, status, error=error)

    async def _save_reply(
        self,
        session_maker: Any,
        conversation_id: int,
        run_id: int,
        text: str,
        tool_parts: list[dict[str, Any]],
        document_parts: list[dict[str, Any]] | None = None,
    ) -> None:
        async with session_maker() as session:
            conv = await session.get(Conversation, conversation_id)
            if conv is None:
                return
            parts = [
                *tool_parts,
                {"type": "text", "text": text},
                *(document_parts or []),
            ]
            msg = await conversations.post_assistant_message(
                session, conv, parts, run_id
            )
            out = _message_dict(msg)
        await self._hub.publish(
            ChannelEvent("message.created", conversation_id, {"message": out})
        )
        memory_indexer.kick()

    async def _maybe_tts_reply(
        self, conversation_id: int, user: User, text: str
    ) -> None:
        """Voice-quick sessions with voice_quick.tts_reply get the reply
        voiced through the `tts` capability as a follow-up audio message."""
        if not text.strip():
            return
        try:
            async with get_sessionmaker()() as session:
                conv = await session.get(Conversation, conversation_id)
                if conv is None or conv.origin != "voice_quick":
                    return
                if not await settings_service.get_value(
                    session, user, "voice_quick.tts_reply", False
                ):
                    return
                audio = await tts.synthesize_speech(
                    session, user, text[:TTS_TEXT_MAX]
                )
            if audio is None:
                return
            data, mime = audio
            name = "reply.mp3" if mime == "audio/mpeg" else "reply.wav"
            async with get_sessionmaker()() as session:
                att, _ = await media_service.create_attachment(
                    session, user, name, mime, data, conversation_id
                )
                att.derived = {"status": "done"}
                conv = await session.get(Conversation, conversation_id)
                if conv is None:
                    return
                msg = await conversations.post_assistant_message(
                    session,
                    conv,
                    [
                        {
                            "type": "audio",
                            "attachment_id": att.id,
                            "tts": True,
                            "name": name,
                            "mime": mime,
                        }
                    ],
                    None,
                )
                out = _message_dict(msg)
            await self._hub.publish(
                ChannelEvent(
                    "message.created", conversation_id, {"message": out}
                )
            )
        except Exception:
            log.exception("tts reply failed conv=%s", conversation_id)

    async def _ensure_title(
        self, conversation_id: int, user: User, parts: list[dict[str, Any]]
    ) -> None:
        text = message_text(parts)
        new_title = await title.generate(conversation_id, user, text)
        if new_title:
            await self._hub.publish(
                ChannelEvent(
                    "conversation.updated",
                    conversation_id,
                    {"conversation": {"id": conversation_id, "title": new_title}},
                )
            )


def _message_dict(m: Message) -> dict[str, Any]:
    """WS payload for a message (same shape as the REST MessageOut)."""
    return {
        "id": m.id,
        "conversation_id": m.conversation_id,
        "seq": m.seq,
        "author_kind": m.author_kind,
        "author_id": m.author_id,
        "parts": m.parts,
        "run_id": m.run_id,
        "source_channel": m.source_channel,
        "client_msg_id": m.client_msg_id,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


_runtime = AgentRuntime(get_hub())


def get_runtime() -> AgentRuntime:
    return _runtime
