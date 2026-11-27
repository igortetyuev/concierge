"""Submissions to `show_checklist` checklist parts (PWA /checklist, TG `cs:`)."""

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.channels import ChannelEvent, get_hub
from concierge.conversations import service as conversations
from concierge.conversations.models import Conversation, Message
from concierge.core.db import get_sessionmaker, utcnow
from concierge.memory import indexer as memory_indexer

log = logging.getLogger(__name__)


class ChecklistError(ValueError):
    """Bad message/checklist reference or an unknown item id."""


def render_state(part: dict[str, Any]) -> str:
    """Human-readable checkbox state stored on the user answer message."""
    title = str(part.get("title") or "чек-лист")
    marks = "; ".join(
        f"{'☑' if it.get('checked') else '☐'} {it.get('text', '')}"
        for it in part.get("items") or []
    )
    return f"Отмечено в «{title}»: {marks}"


def compact_followup_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Display copy of a hidden follow-up reply embedded in the checklist
    part: text and thinking verbatim, tool calls collapsed to chips (full
    args/returns stay in the hidden reply row only)."""
    failed = {
        str(p.get("tool_call_id"))
        for p in parts
        if p.get("type") == "tool_return"
        and (
            p.get("retry") is True
            or p.get("outcome") in ("failed", "denied")
        )
    }
    out: list[dict[str, Any]] = []
    for p in parts:
        ptype = p.get("type")
        if ptype == "text" and p.get("text"):
            out.append({"type": "text", "text": str(p["text"])})
        elif ptype == "thinking" and p.get("content"):
            out.append({"type": "thinking", "content": str(p["content"])})
        elif ptype == "tool_call":
            out.append(
                {
                    "type": "tool",
                    "name": str(p.get("name") or "tool"),
                    "status": (
                        "error"
                        if str(p.get("tool_call_id")) in failed
                        else "done"
                    ),
                }
            )
    return out


_HOMEWORK_IMPORT_TOOL = "analytics_import_rows"
_HOMEWORK_TABLE = "school_homework_done"
_HOMEWORK_RESULT_TABLE = "sandbox.school_homework_done"


def extract_school_homework_done_updates(
    parts: list[dict[str, Any]],
) -> dict[str, bool]:
    """Extract confirmed homework-done writes from a stored agent run log.

    Only a successful `analytics_import_rows` call/return pair is accepted:
    append into `school_homework_done`, structured result for
    `sandbox.school_homework_done`, and exactly all input rows inserted.
    Invalid rows are skipped; for duplicate hw_ids the later write wins.
    """
    calls: dict[str, list[Any]] = {}
    updates: dict[str, bool] = {}
    for part in parts:
        call_id = part.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        if part.get("type") == "tool_call":
            if part.get("name") != _HOMEWORK_IMPORT_TOOL:
                continue
            raw_args = part.get("args")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except ValueError:
                    continue
            else:
                args = raw_args
            if not isinstance(args, dict):
                continue
            if args.get("table") != _HOMEWORK_TABLE:
                continue
            if args.get("mode", "append") != "append":
                continue
            rows = args.get("rows")
            if isinstance(rows, list):
                calls[call_id] = rows
            continue
        if part.get("type") != "tool_return":
            continue
        rows = calls.get(call_id)
        if rows is None or part.get("name") != _HOMEWORK_IMPORT_TOOL:
            continue
        if part.get("retry") is True:
            continue
        if part.get("outcome") not in (None, "success"):
            continue
        content = part.get("content")
        if not isinstance(content, dict):
            continue
        if content.get("table") != _HOMEWORK_RESULT_TABLE:
            continue
        inserted = content.get("inserted")
        if isinstance(inserted, bool) or not isinstance(inserted, int):
            continue
        if inserted <= 0 or inserted != len(rows):
            continue
        calls.pop(call_id, None)
        for row in rows:
            if not isinstance(row, dict):
                continue
            hw_id = row.get("hw_id")
            done = row.get("done")
            if not isinstance(hw_id, str) or not hw_id.strip():
                continue
            if type(done) is not bool:
                continue
            updates[hw_id] = done
    return updates


def _has_homework_checklist_item(
    parts: list[dict[str, Any]], updates: dict[str, bool]
) -> bool:
    for part in parts:
        if part.get("type") != "checklist":
            continue
        for item in part.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id in updates:
                return True
    return False


def _rewrite_homework_checklist_parts(
    parts: list[dict[str, Any]], updates: dict[str, bool]
) -> tuple[list[dict[str, Any]], bool]:
    changed = False
    new_parts: list[dict[str, Any]] = []
    for part in parts:
        if part.get("type") != "checklist":
            new_parts.append(part)
            continue
        items_changed = False
        new_items: list[Any] = []
        for item in part.get("items") or []:
            if not isinstance(item, dict):
                new_items.append(item)
                continue
            item_id = item.get("id")
            if (
                isinstance(item_id, str)
                and item_id in updates
                and bool(item.get("checked")) != updates[item_id]
            ):
                item = {**item, "checked": updates[item_id]}
                items_changed = True
            new_items.append(item)
        if items_changed:
            changed = True
            new_parts.append({**part, "items": new_items})
        else:
            new_parts.append(part)
    return new_parts, changed


async def sync_school_homework_done_checklists(
    conversation_id: int, updates: dict[str, bool]
) -> int:
    """Apply extracted homework states to visible checklist cards in a conversation.

    Candidate rows are locked FOR UPDATE and rechecked before rewriting only
    `items[].checked`; submission metadata/followups and all unrelated fields
    are preserved. Returns the number of rewritten messages. Realtime events
    are published only after the commit.
    """
    if not updates:
        return 0
    changed_msgs: list[Message] = []
    async with get_sessionmaker()() as session:
        visible = (
            await session.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.author_kind == "assistant",
                    Message.hidden.is_(False),
                )
                .order_by(Message.seq)
            )
        ).scalars().all()
        candidate_ids = [
            msg.id
            for msg in visible
            if _has_homework_checklist_item(msg.parts, updates)
        ]
        if not candidate_ids:
            return 0
        locked = (
            await session.execute(
                select(Message)
                .where(Message.id.in_(candidate_ids))
                .order_by(Message.seq)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        for msg in locked:
            if (
                msg.conversation_id != conversation_id
                or msg.author_kind != "assistant"
                or msg.hidden
            ):
                continue
            new_parts, changed = _rewrite_homework_checklist_parts(
                msg.parts, updates
            )
            if changed:
                msg.parts = new_parts
                changed_msgs.append(msg)
        if changed_msgs:
            await session.commit()
    if changed_msgs:
        # Late import: agent.runtime -> skills.tools would circular-import.
        from concierge.agent.runtime import _message_dict

        hub = get_hub()
        for msg in changed_msgs:
            await hub.publish(
                ChannelEvent(
                    "message.updated",
                    conversation_id,
                    {"message": _message_dict(msg)},
                )
            )
    return len(changed_msgs)


async def publish_answer(conv_id: int, msg: Message, reply: Message) -> None:
    """Fan out the part update + answer message and schedule the next run.

    Shared by choice answers and checklist submissions.
    """
    # Late import: agent.runtime -> skills.tools would circular-import.
    from concierge.agent.runtime import _message_dict, get_runtime

    reply_out = _message_dict(reply)
    msg_out = _message_dict(msg)
    hub = get_hub()
    await hub.publish(
        ChannelEvent("message.updated", conv_id, {"message": msg_out})
    )
    if not reply.hidden:
        await hub.publish(
            ChannelEvent("message.created", conv_id, {"message": reply_out})
        )
    memory_indexer.kick()
    get_runtime().schedule(conv_id, reply.id)


async def submit_checklist(
    session: AsyncSession,
    conv: Conversation,
    user: User,
    message_id: int,
    checklist_id: str,
    checked: list[str],
    source_channel: str,
) -> Message:
    """Record a checklist submission and post the user's answer message.

    Raises ChecklistError("not_found" | "unknown_item"). Resubmission is
    allowed, but a re-sent identical state is deduplicated: the previous
    answer message comes back without a new run. The message row is locked
    FOR UPDATE so racing submits serialize instead of losing updates. The
    caller's session must already have verified the user's membership in
    `conv`.
    """
    msg = (
        await session.execute(
            select(Message)
            .where(Message.id == message_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        msg is None
        or msg.conversation_id != conv.id
        or msg.author_kind != "assistant"
    ):
        raise ChecklistError("not_found")
    part = next(
        (
            p
            for p in msg.parts
            if p.get("type") == "checklist"
            and p.get("checklist_id") == checklist_id
        ),
        None,
    )
    if part is None:
        raise ChecklistError("not_found")
    items = part.get("items") or []
    wanted = {str(i) for i in checked}
    known = {str(it.get("id")) for it in items}
    if not wanted <= known:
        raise ChecklistError("unknown_item")
    changed = [
        {"id": str(it.get("id")), "checked": str(it.get("id")) in wanted}
        for it in items
        if bool(it.get("checked")) != (str(it.get("id")) in wanted)
    ]
    submissions = int(part.get("submissions") or 0)
    if submissions > 0 and not changed:
        # Same state re-sent (double-tap, stale keyboard): reuse the stored
        # key so post_message dedup returns the existing answer — no extra
        # message or run.
        new_part = part
    else:
        submissions += 1
        new_items = [
            {**it, "checked": str(it.get("id")) in wanted} for it in items
        ]
        # The follow-up exchange (this answer + the agent's reply) is folded
        # into the card's "рассуждение": a pending entry appears right away,
        # the runtime fills its `parts` when the hidden reply run finishes.
        followups = [
            *(part.get("followups") or []),
            {
                "n": submissions,
                "answer": render_state({**part, "items": new_items}),
                "at": utcnow().isoformat(timespec="seconds"),
                "parts": None,
            },
        ]
        new_part = {
            **part,
            "items": new_items,
            "submissions": submissions,
            "submitted_by": user.id,
            "submitted_at": utcnow().isoformat(timespec="seconds"),
            "followups": followups,
        }
        msg.parts = [new_part if p is part else p for p in msg.parts]
    reply, created = await conversations.post_message(
        session,
        conv,
        user,
        [
            {
                "type": "text",
                "text": render_state(new_part),
                "checklist_answer": {
                    "message_id": message_id,
                    "checklist_id": checklist_id,
                    "n": submissions,
                    "skill": part.get("skill"),
                    "skill_id": part.get("skill_id"),
                    "items": [
                        {
                            "id": str(it.get("id")),
                            "checked": str(it.get("id")) in wanted,
                        }
                        for it in items
                    ],
                    "changed": changed,
                },
            }
        ],
        f"cl:{message_id}:{submissions}:{checklist_id[:8]}",
        source_channel=source_channel,
        hidden=True,
    )
    await session.commit()
    if created:
        await publish_answer(conv.id, msg, reply)
    return reply
