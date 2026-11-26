"""Submissions to `show_checklist` checklist parts (PWA /checklist, TG `cs:`)."""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.channels import ChannelEvent, get_hub
from concierge.conversations import service as conversations
from concierge.conversations.models import Conversation, Message
from concierge.core.db import utcnow
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
        new_part = {
            **part,
            "items": [
                {**it, "checked": str(it.get("id")) in wanted}
                for it in items
            ],
            "submissions": submissions,
            "submitted_by": user.id,
            "submitted_at": utcnow().isoformat(timespec="seconds"),
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
