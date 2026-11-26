import asyncio
import dataclasses
import json
import logging
from typing import Any

import httpx
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
    ThinkingPart,
    ThinkingPartDelta,
    UserPromptPart,
)
from pydantic_core import to_jsonable_python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.agent import history as agent_history
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
from concierge.skills import activation as skill_activation
from concierge.skills import format as skill_format
from concierge.skills import service as skill_service
from concierge.skills import tools as skill_tools

log = logging.getLogger(__name__)

HISTORY_LIMIT = 100
MEDIA_TEXT_MAX = 4000

# Auto-compaction: once estimated context usage reaches COMPACTION_THRESHOLD
# of the binding's params.context_length, older messages are summarized into
# a `compaction` marker message and only the tail is replayed verbatim.
COMPACTION_THRESHOLD = 0.75
COMPACTION_KEEP_TAIL = 12
COMPACTION_INPUT_MAX = 200_000
COMPACTION_SUMMARY_MAX = 8000
COMPACTION_INSTRUCTIONS = (
    "Сожми историю диалога в плотный конспект для продолжения работы "
    "агента-консьержа. Сохрани: решения и договорённости, незавершённые "
    "задачи, важные факты (имена, цифры, даты, ссылки, идентификаторы), "
    "контекст последних вопросов. Пиши по-русски, короткими пунктами, "
    "без вступлений."
)

ERROR_TEXT = "Произошла ошибка при обработке запроса."


class AgentRuntime:
    """Runs the PydanticAI agent for a trigger message; one run per session."""

    def __init__(self, hub: Hub) -> None:
        self._hub = hub
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(
        self, conversation_id: int, trigger_message_id: int, wait: bool = False
    ) -> None:
        task = asyncio.create_task(
            self.maybe_run(conversation_id, trigger_message_id, wait=wait)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """Await all currently scheduled runs (tests, graceful shutdown)."""
        while self._tasks:
            await asyncio.gather(*self._tasks)

    async def maybe_run(
        self, conversation_id: int, trigger_message_id: int, wait: bool = False
    ) -> None:
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        if lock.locked() and not wait:
            # A /stop, /skill command or interactive-part answer sent
            # mid-run must not be lost: re-read the trigger and requeue
            # it with wait=True.
            if not await self._must_requeue(trigger_message_id):
                return  # one run per conversation at a time
            wait = True
        async with lock:
            try:
                await self._run(conversation_id, trigger_message_id)
            except Exception:
                log.exception("agent run failed conv=%s", conversation_id)

    async def _must_requeue(self, trigger_message_id: int) -> bool:
        async with get_sessionmaker()() as session:
            trigger = await session.get(Message, trigger_message_id)
            if trigger is None:
                return False
            return (
                skill_activation.parse_command(trigger.parts) is not None
                or _is_interactive_reply(trigger.parts)
            )

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
            hidden_reply = trigger.hidden and _is_checklist_reply(trigger.parts)
            # /stop never creates a run: it only unpins the active skill.
            cmd = skill_activation.parse_command(trigger.parts)
            if cmd is not None and cmd[0] == "stop":
                msg = await skill_service.deactivate(
                    session, conv, user, "stop"
                )
                if msg is not None:
                    await self._emit(
                        "message.created",
                        conv.id,
                        {"message": _message_dict(msg)},
                    )
                return
            activated = False
            # An ephemeral skill (/command, trigger or use_skill) applies to
            # this run only: no `active_skill` state, no `skill_event`.
            ephemeral_state: tuple[Any, str] | None = None
            if cmd is not None and (
                skill := await skill_service.resolve(session, user, cmd[1])
            ) is not None:
                activated = True
                if skill.ephemeral:
                    ephemeral_state = (skill, cmd[2])
                else:
                    msg = await skill_service.activate(
                        session, conv, skill, cmd[2], user
                    )
                    await self._emit(
                        "message.created",
                        conv.id,
                        {"message": _message_dict(msg)},
                    )
            # A choice/checklist answer continues the flow that produced the
            # part: it counts as a mention (wakes reply_mode='mention') and
            # skips phrase triggers, and the skill recorded on the part is
            # re-applied — ephemeral ones get injected for this run only.
            if not activated and _is_interactive_reply(trigger.parts):
                activated = True
                skill_id, skill_name = _answer_skill(trigger.parts)
                skill = None
                if skill_id is not None:
                    # Resolve by id among the answering user's skills: a
                    # same-named skill of the answerer must not shadow the
                    # one that produced the part.
                    skill = next(
                        (
                            s
                            for s in await skill_service.available_skills(
                                session, user
                            )
                            if s.id == skill_id
                        ),
                        None,
                    )
                elif skill_name is not None:
                    skill = await skill_service.resolve(
                        session, user, skill_name
                    )
                if skill is not None and skill.ephemeral:
                    ephemeral_state = (skill, "")
            if (
                conv.reply_mode == "mention"
                and not activated
                and not _is_scheduled(trigger.parts)
                and not prompt.is_mentioned(
                    trigger.parts, await prompt.mentions(session, user)
                )
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
                session,
                conv.id,
                before_seq=trigger.seq,
                limit=HISTORY_LIMIT,
                include_hidden=True,
            )
            atts = await self._attachments(session, [*history_rows, trigger])
            trigger_text = self._content(
                trigger.parts, conv, member_names, user.id, atts
            )
            # Phrase triggers fire only when no skill is already pinned.
            active = await skill_service.active_skill_state(session, conv)
            if not activated and active is None:
                plain = " ".join(
                    str(p.get("text", ""))
                    for p in trigger.parts
                    if p.get("type") == "text"
                )
                for s in await skill_service.available_skills(session, user):
                    if s.triggers and skill_format.match_trigger(
                        s.triggers, plain
                    ):
                        if s.ephemeral:
                            ephemeral_state = (s, "")
                        else:
                            msg = await skill_service.activate(
                                session, conv, s, "", user
                            )
                            await self._emit(
                                "message.created",
                                conv.id,
                                {"message": _message_dict(msg)},
                            )
                            active = (s, "")
                        break
            active_skill_chunk = (
                skill_activation.ephemeral_skill_chunk(*ephemeral_state)
                if ephemeral_state is not None
                else (
                    skill_activation.active_skill_chunk(*active)
                    if active is not None
                    else None
                )
            )
            memory_block = await memory_service.retrieve_context(
                session, user, conv, trigger_text
            )
            allowed_names = tools.allowed_tool_names(user.role.permissions)
            skills_catalog = None
            if "use_skill" in allowed_names:
                skills_catalog = skill_service.catalog_block(
                    await skill_service.available_skills(session, user)
                ) or None
            instructions = await prompt.system_prompt(
                session,
                user,
                conv,
                list(member_names.values()),
                memory_block,
                skills_catalog=skills_catalog,
                active_skill=active_skill_chunk,
            )
            toolsets = await mcp.build_toolsets(session, user)
            funcs = tools.core_tools(user.role.permissions)
            if "ask_model" in allowed_names and (
                named_names := await agent_service.enabled_named_model_names(
                    session
                )
            ):
                funcs.append(tools.make_ask_model_tool(named_names))
            if "use_skill" in allowed_names and (
                skill_names := [
                    s.name
                    for s in await skill_service.available_skills(
                        session, user
                    )
                ]
            ):
                funcs.append(skill_tools.make_use_skill_tool(skill_names))
            voice_ui = conv.origin == "voice_quick" or bool(
                (conv.settings or {}).get("voice_session")
            )
            for name, tool_fn in (
                ("end_skill", skill_tools.end_skill),
                ("read_skill_file", skill_tools.read_skill_file),
                ("save_skill", skill_tools.save_skill),
                ("ask_user_question", tools.ask_user_question),
                ("show_checklist", tools.show_checklist),
                ("run_subagent", tools.run_subagent),
            ):
                # Checklist UI has no voice rendering.
                if name == "show_checklist" and voice_ui:
                    continue
                if name in allowed_names:
                    funcs.append(tool_fn)
            capabilities = tools.allowed_capabilities(user.role.permissions)
            prefix: list[ModelMessage] = []
            if (
                bound is not None
                and bound.context_length
                and len(history_rows) > COMPACTION_KEEP_TAIL
            ):
                est = await self._estimate_context(
                    session, conv.id, history_rows
                )
                if est >= bound.context_length * COMPACTION_THRESHOLD:
                    prefix, history_rows = await self._compact(
                        session,
                        conv,
                        history_rows,
                        bound,
                        user,
                        member_names,
                        est,
                    )
            history = prefix + self._build_history(
                history_rows, conv, member_names, atts
            )
            deps = ToolDeps(
                user_id=user.id,
                conversation_id=conv.id,
                trigger_seq=trigger.seq,
                subagent_slots=asyncio.Semaphore(5),
                skill_name=(
                    ephemeral_state[0].name
                    if ephemeral_state is not None
                    else (active[0].name if active is not None else None)
                ),
                skill_id=(
                    ephemeral_state[0].id
                    if ephemeral_state is not None
                    else (active[0].id if active is not None else None)
                ),
            )

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
            if not hidden_reply:
                await self._run_status(
                    conv.id, run_id, "running", model=run.model
                )

            async def emit_run_event(
                event_type: str, conversation_id: int, data: dict[str, Any]
            ) -> None:
                if not hidden_reply:
                    await self._emit(event_type, conversation_id, data)

        if bound is None:
            await self._finish(
                run_id,
                conv.id,
                "failed",
                error="no chat model bound",
                quiet=hidden_reply,
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
        # Fallback parts collected from stream events; on success the stored
        # parts come from run_result.new_messages() instead (full fidelity:
        # thinking blocks, provider signatures, tool args and results).
        run_parts: list[dict[str, Any]] = []
        run_result: Any = None
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
                        await emit_run_event(
                            "message.delta",
                            conv.id,
                            {"run_id": run_id, "text": delta_text},
                        )
                    # Thinking parts stream through a separate event so the
                    # client can fill its "рассуждение" block; signature-only
                    # deltas carry no text and are only folded into run_parts.
                    if isinstance(event, PartStartEvent) and isinstance(
                        event.part, ThinkingPart
                    ):
                        tp = event.part
                        entry: dict[str, Any] = {
                            "type": "thinking",
                            "content": tp.content,
                        }
                        if tp.signature is not None:
                            entry["signature"] = to_jsonable_python(
                                tp.signature
                            )
                        run_parts.append(entry)
                        await emit_run_event(
                            "message.thinking",
                            conv.id,
                            {
                                "run_id": run_id,
                                "text": tp.content,
                                "start": True,
                            },
                        )
                    elif (
                        isinstance(event, PartDeltaEvent)
                        and isinstance(event.delta, ThinkingPartDelta)
                    ):
                        td = event.delta
                        if run_parts and run_parts[-1].get("type") == "thinking":
                            last = run_parts[-1]
                            if td.content_delta:
                                last["content"] = (
                                    str(last.get("content", ""))
                                    + td.content_delta
                                )
                            if td.signature_delta:
                                last["signature"] = str(
                                    last.get("signature", "")
                                ) + td.signature_delta
                        if td.content_delta:
                            await emit_run_event(
                                "message.thinking",
                                conv.id,
                                {
                                    "run_id": run_id,
                                    "text": td.content_delta,
                                },
                            )
                    if isinstance(event, FunctionToolCallEvent):
                        call = event.part
                        run_parts.append(
                            {
                                "type": "tool_call",
                                "name": call.tool_name,
                                "tool_call_id": call.tool_call_id,
                                "args": to_jsonable_python(call.args),
                            }
                        )
                        await emit_run_event(
                            "message.tool",
                            conv.id,
                            {
                                "run_id": run_id,
                                "type": "tool",
                                "name": call.tool_name,
                                "tool_call_id": call.tool_call_id,
                                "status": "running",
                            },
                        )
                    elif (
                        isinstance(event, PartStartEvent)
                        and isinstance(event.part, NativeToolCallPart)
                    ):
                        # Provider-side tools (e.g. web_search) arrive as part
                        # events, not FunctionToolCallEvent.
                        ncall = event.part
                        run_parts.append(
                            {
                                "type": "tool_call",
                                "name": ncall.tool_name,
                                "tool_call_id": ncall.tool_call_id or "",
                                "args": to_jsonable_python(ncall.args),
                                "native": True,
                            }
                        )
                        await emit_run_event(
                            "message.tool",
                            conv.id,
                            {
                                "run_id": run_id,
                                "type": "tool",
                                "name": ncall.tool_name,
                                "tool_call_id": ncall.tool_call_id or "",
                                "status": "running",
                            },
                        )
                    elif (
                        isinstance(event, PartStartEvent)
                        and isinstance(event.part, NativeToolReturnPart)
                    ):
                        nret = event.part
                        run_parts.append(
                            {
                                "type": "tool_return",
                                "name": nret.tool_name,
                                "tool_call_id": nret.tool_call_id or "",
                                "content": to_jsonable_python(nret.content),
                                "native": True,
                            }
                        )
                        await emit_run_event(
                            "message.tool",
                            conv.id,
                            {
                                "run_id": run_id,
                                "type": "tool",
                                "name": nret.tool_name,
                                "tool_call_id": nret.tool_call_id,
                                "status": "done",
                            },
                        )
                    elif isinstance(event, FunctionToolResultEvent):
                        res = event.part
                        retry = isinstance(res, RetryPromptPart)
                        ret: dict[str, Any] = {
                            "type": "tool_return",
                            "name": getattr(res, "tool_name", None) or "tool",
                            "tool_call_id": res.tool_call_id,
                            "content": to_jsonable_python(
                                getattr(res, "content", None)
                            ),
                        }
                        if retry:
                            ret["retry"] = True
                        run_parts.append(ret)
                        await emit_run_event(
                            "message.tool",
                            conv.id,
                            {
                                "run_id": run_id,
                                "type": "tool",
                                "name": getattr(res, "tool_name", None)
                                or "tool",
                                "tool_call_id": res.tool_call_id,
                                "status": "error" if retry else "done",
                            },
                        )
                    elif isinstance(event, AgentRunResultEvent):
                        run_result = event.result
                        output = str(event.result.output)
                        usage = dataclasses.asdict(event.result.usage)
        except Exception as exc:
            log.exception("run %s failed", run_id)
            await self._finish(
                run_id, conv.id, "failed", error=str(exc), quiet=hidden_reply
            )
            await self._save_reply(
                session_maker,
                conv.id,
                run_id,
                [*run_parts, {"type": "text", "text": ERROR_TEXT}],
                deps.documents,
                deps.choices,
                deps.checklists,
                hidden=hidden_reply,
            )
            return

        await self._finish(
            run_id, conv.id, "done", usage=usage, quiet=hidden_reply
        )
        stored_parts = (
            agent_history.serialize_parts(run_result.new_messages())
            if run_result is not None
            else []
        )
        if not stored_parts:
            stored_parts = [
                *run_parts,
                {"type": "text", "text": output},
            ]
        await self._save_reply(
            session_maker,
            conv.id,
            run_id,
            stored_parts,
            deps.documents,
            deps.choices,
            deps.checklists,
            hidden=hidden_reply,
        )
        if not hidden_reply:
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
            marker = next(
                (p for p in m.parts if p.get("type") == "compaction"), None
            )
            if marker is not None:
                # Everything before the latest marker was folded into its
                # summary — replay from here.
                out = [
                    ModelRequest(
                        parts=[
                            UserPromptPart(
                                agent_history.compaction_prompt(marker)
                            )
                        ]
                    )
                ]
                continue
            if m.author_kind == "assistant":
                rebuilt = agent_history.deserialize_parts(m.parts)
                if rebuilt:
                    out.extend(rebuilt)
                    continue
            text = self._content(
                m.parts, conv, member_names, m.author_id, atts
            )
            if not text:
                continue
            if m.author_kind == "assistant":
                out.append(ModelResponse(parts=[TextPart(text)]))
            else:
                out.append(ModelRequest(parts=[UserPromptPart(text)]))
        return out

    async def _estimate_context(
        self,
        session: AsyncSession,
        conversation_id: int,
        rows: list[Message],
    ) -> int:
        """Estimated context usage in tokens: the better of the last run's
        real input_tokens and a chars-based estimate of stored history."""
        last = (
            await session.execute(
                select(Run)
                .where(
                    Run.conversation_id == conversation_id,
                    Run.status == "done",
                )
                .order_by(Run.id.desc())
                .limit(1)
            )
        ).scalars().first()
        prev_input = int((last.usage or {}).get("input_tokens") or 0) if last else 0
        # Only what a replay would contain: the last marker's summary plus
        # everything after it (earlier rows are superseded, not dropped).
        marker_idx = max(
            (
                i
                for i, m in enumerate(rows)
                if any(p.get("type") == "compaction" for p in m.parts)
            ),
            default=-1,
        )
        effective = rows[marker_idx:] if marker_idx >= 0 else rows
        stored = sum(
            agent_history.estimate_parts_tokens(m.parts) for m in effective
        )
        return max(prev_input, stored)

    async def _compact(
        self,
        session: AsyncSession,
        conv: Conversation,
        rows: list[Message],
        bound: providers.BoundModel,
        user: User,
        member_names: dict[int, str],
        tokens_before: int,
    ) -> tuple[list[ModelMessage], list[Message]]:
        """Summarize all but the tail rows into a `compaction` marker message.

        Returns (history prefix carrying the summary, remaining tail rows).
        On any failure the original rows are kept untouched.
        """
        # Fold any previous marker into the new summary rather than leaving
        # it inside the replayed tail.
        marker_idx = max(
            (
                i
                for i, m in enumerate(rows)
                if any(p.get("type") == "compaction" for p in m.parts)
            ),
            default=-1,
        )
        effective = rows[marker_idx:] if marker_idx >= 0 else rows
        if len(effective) <= COMPACTION_KEEP_TAIL:
            return [], rows
        tail = effective[-COMPACTION_KEEP_TAIL:]
        old = effective[:-COMPACTION_KEEP_TAIL]
        rendered = "\n\n".join(
            f"{member_names.get(m.author_id) if m.author_id is not None else m.author_kind}: "
            f"{agent_history.render_for_summary(m.parts)}"
            for m in old
        )
        try:
            # Summarization is a utility call — prefer the cheap chat_fast
            # capability, falling back to the chat model itself.
            summarizer = await providers.get_model(
                session, "chat_fast", user
            )
            agent = Agent(
                (summarizer or bound).model,
                instructions=COMPACTION_INSTRUCTIONS,
                model_settings=(summarizer or bound).settings,
            )
            result = await agent.run(rendered[:COMPACTION_INPUT_MAX])
            summary = (
                str(result.output).strip()[:COMPACTION_SUMMARY_MAX]
                or rendered[:COMPACTION_SUMMARY_MAX]
            )
        except Exception:
            log.exception("context compaction failed conv=%s", conv.id)
            return [], rows
        marker = await conversations.post_assistant_message(
            session,
            conv,
            [
                agent_history.compaction_part(
                    summary, old[-1].seq, len(old), tokens_before
                )
            ],
            None,
            "system",
        )
        await self._hub.publish(
            ChannelEvent(
                "message.created", conv.id, {"message": _message_dict(marker)}
            )
        )
        log.info(
            "context compacted conv=%s: %s msgs -> summary (~%s tokens)",
            conv.id,
            len(old),
            tokens_before,
        )
        prefix: list[ModelMessage] = [
            ModelRequest(
                parts=[
                    UserPromptPart(
                        agent_history.compaction_prompt(marker.parts[0])
                    )
                ]
            )
        ]
        return prefix, tail

    @staticmethod
    def _content(
        parts: list[dict[str, Any]],
        conv: Conversation,
        member_names: dict[int, str],
        author_id: int | None,
        atts: dict[int, Attachment],
    ) -> str:
        chunks = []
        speaker_label: str | None = None
        for p in parts:
            ptype = p.get("type")
            if ptype == "text":
                sched = p.get("scheduled")
                if isinstance(sched, dict):
                    jid = sched.get("job_id")
                    stitle = str(sched.get("title") or "")
                    if sched.get("kind") == "reminder":
                        chunks.append(
                            f"[Сработало напоминание #{jid} «{stitle}»] "
                            f"Напомни пользователю: {p.get('text', '')}"
                        )
                    else:
                        chunks.append(
                            f"[Событие по расписанию #{jid} «{stitle}»] "
                            f"{p.get('text', '')}"
                        )
                else:
                    chunks.append(str(p.get("text", "")))
                cl_ans = p.get("checklist_answer")
                if isinstance(cl_ans, dict):
                    state = {
                        "items": {
                            str(i.get("id")): bool(i.get("checked"))
                            for i in cl_ans.get("items") or []
                        },
                        "changed": [
                            str(i.get("id"))
                            for i in cl_ans.get("changed") or []
                        ],
                    }
                    chunks.append(
                        f"[чек-лист: {json.dumps(state, ensure_ascii=False)}]"
                    )
                if speaker_label is None:
                    sp = p.get("speaker")
                    if isinstance(sp, dict):
                        speaker_label = str(sp.get("name") or "") or None
            elif ptype == "skill_event":
                verb = (
                    "запущен"
                    if p.get("action") == "start"
                    else "завершён"
                )
                chunks.append(f"[Скилл «{p.get('name', '?')}» {verb}]")
            elif ptype in (
                "tool",
                "tool_call",
                "tool_return",
                "thinking",
                "compaction",
                "mention",
                "choice",
                "checklist",
            ):
                continue
            elif (aid := media_service.part_attachment_id(p)) is not None and (
                att := atts.get(aid)
            ) is not None:
                if speaker_label is None:
                    sp = att.derived.get("speaker")
                    if isinstance(sp, dict):
                        speaker_label = str(sp.get("name") or "") or None
                label = (
                    "голосовой ответ"
                    if p.get("tts")
                    else att.original_name or ptype or "attachment"
                )
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
            who = speaker_label or member_names.get(author_id, "unknown")
            text = f"{who}: {text}"
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
        quiet: bool = False,
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
        if not quiet:
            await self._run_status(
                conversation_id, run_id, status, error=error
            )

    async def _save_reply(
        self,
        session_maker: Any,
        conversation_id: int,
        run_id: int,
        parts: list[dict[str, Any]],
        document_parts: list[dict[str, Any]] | None = None,
        choice_parts: list[dict[str, Any]] | None = None,
        checklist_parts: list[dict[str, Any]] | None = None,
        hidden: bool = False,
    ) -> None:
        async with session_maker() as session:
            conv = await session.get(Conversation, conversation_id)
            if conv is None:
                return
            msg = await conversations.post_assistant_message(
                session,
                conv,
                [
                    *parts,
                    *(document_parts or []),
                    *(choice_parts or []),
                    *(checklist_parts or []),
                ],
                run_id,
                hidden=hidden,
            )
            out = _message_dict(msg)
        if not hidden:
            await self._hub.publish(
                ChannelEvent(
                    "message.created", conversation_id, {"message": out}
                )
            )
        memory_indexer.kick()

    async def _maybe_tts_reply(
        self, conversation_id: int, user: User, text: str
    ) -> None:
        """Voice sessions get the reply voiced through the `tts` capability
        as a follow-up audio message; on a speaker line (voice_session)
        the audio is also pushed to the esp32's /play endpoint."""
        if not text.strip():
            return
        try:
            speaker: tuple[str, str | None] | None = None
            async with get_sessionmaker()() as session:
                conv = await session.get(Conversation, conversation_id)
                if conv is None:
                    return
                voice_line = bool(conv.settings.get("voice_session"))
                if conv.origin == "voice_quick":
                    enabled = await settings_service.get_value(
                        session, user, "voice_quick.tts_reply", False
                    )
                elif voice_line:
                    enabled = await settings_service.get_value(
                        session, user, "voice_session.tts_reply", True
                    )
                else:
                    return
                if not enabled:
                    return
                spoken = await tts.spoken_version(session, user, text)
                if not spoken:
                    return
                audio = await tts.synthesize_speech(session, user, spoken)
                if voice_line:
                    speaker = await _speaker_endpoint(session)
            if audio is None:
                return
            data, mime = audio
            name = "reply.mp3" if mime == "audio/mpeg" else "reply.wav"
            async with get_sessionmaker()() as session:
                att, _ = await media_service.create_attachment(
                    session, user, name, mime, data, conversation_id
                )
                att.derived = {"status": "done", "spoken": spoken}
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
            if speaker is not None:
                await _speaker_play(*speaker, att.id)
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


def _is_scheduled(parts: list[dict[str, Any]]) -> bool:
    """A scheduler trigger message (fired event/reminder) counts as a mention
    so it wakes a reply_mode='mention' session too."""
    return any(
        p.get("type") == "text" and isinstance(p.get("scheduled"), dict)
        for p in parts
    )


def _is_checklist_reply(parts: list[dict[str, Any]]) -> bool:
    return any(
        p.get("type") == "text" and isinstance(p.get("checklist_answer"), dict)
        for p in parts
    )


def _is_interactive_reply(parts: list[dict[str, Any]]) -> bool:
    """A choice/checklist answer message counts as a mention and continues
    the flow that produced the part."""
    return any(
        p.get("type") == "text"
        and isinstance(
            p.get("choice_answer") or p.get("checklist_answer"), dict
        )
        for p in parts
    )


def _answer_skill(
    parts: list[dict[str, Any]],
) -> tuple[int | None, str | None]:
    """(skill_id, skill_name) recorded on the interactive part an answer
    refers to; legacy parts carry only the name."""
    for p in parts:
        if p.get("type") != "text":
            continue
        for key in ("checklist_answer", "choice_answer"):
            ans = p.get(key)
            if not isinstance(ans, dict):
                continue
            sid = ans.get("skill_id")
            if isinstance(sid, int) and not isinstance(sid, bool):
                return sid, None
            if ans.get("skill"):
                return None, str(ans["skill"])
    return None, None


async def _speaker_endpoint(
    session: AsyncSession,
) -> tuple[str, str | None] | None:
    """(base_url, token) of the living-room speaker from mcp.env.livingroom."""
    env = await settings_service.get_global(session, "mcp.env.livingroom")
    conf = env if isinstance(env, dict) else {}
    base = str(conf.get("SPEAKER_URL") or "").rstrip("/")
    if not base:
        return None
    token = conf.get("SPEAKER_TOKEN")
    return base, str(token) if token else None


async def _speaker_play(
    base: str, token: str | None, attachment_id: int
) -> None:
    """Ask the esp32 to stream a concierge attachment over HTTP."""
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{base}/play",
                json={"url": f"/api/attachments/{attachment_id}"},
                headers=headers,
            )
        if resp.status_code != 200:
            log.warning(
                "speaker /play att=%s -> %s", attachment_id, resp.status_code
            )
    except Exception:
        log.warning("speaker /play att=%s unreachable", attachment_id)


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
        "hidden": m.hidden,
        "client_msg_id": m.client_msg_id,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


_runtime = AgentRuntime(get_hub())


def get_runtime() -> AgentRuntime:
    return _runtime
