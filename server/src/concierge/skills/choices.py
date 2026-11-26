"""Answers to `ask_user_question` choice parts (PWA /answer, TG `ch:`)."""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.conversations import service as conversations
from concierge.conversations.models import Conversation, Message
from concierge.skills.checklists import publish_answer

log = logging.getLogger(__name__)


class ChoiceError(ValueError):
    """Bad message/choice reference or an already-answered question."""


def render_answers(questions: list[dict[str, Any]], answers: list[dict[str, Any]]) -> str:
    """Human-readable answer text stored on the user message."""
    lines = []
    for i, ans in enumerate(answers):
        q = questions[i] if i < len(questions) else {}
        head = str(q.get("question") or f"Вопрос {i + 1}")
        other = str(ans.get("other") or "").strip()
        selected = [str(s) for s in ans.get("selected") or []]
        picked = f"Другое: {other}" if other else "; ".join(selected)
        lines.append(f"{head} — {picked}")
    return "\n".join(lines)


async def answer_choice(
    session: AsyncSession,
    conv: Conversation,
    user: User,
    message_id: int,
    choice_id: str,
    answers: list[dict[str, Any]],
    source_channel: str,
) -> Message:
    """Mark the choice part answered and post the user's answer message.

    Raises ChoiceError("not_found" | "answered"). The caller's session must
    already have verified the user's membership in `conv`.
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
        raise ChoiceError("not_found")
    part = next(
        (
            p
            for p in msg.parts
            if p.get("type") == "choice" and p.get("choice_id") == choice_id
        ),
        None,
    )
    if part is None:
        raise ChoiceError("not_found")
    if part.get("answered"):
        raise ChoiceError("answered")
    questions = part.get("questions") or []
    parts = [
        (
            {**p, "answered": True, "answers": answers}
            if p is part
            else p
        )
        for p in msg.parts
    ]
    msg.parts = parts
    text = render_answers(questions, answers)
    reply, created = await conversations.post_message(
        session,
        conv,
        user,
        [
            {
                "type": "text",
                "text": text,
                "choice_answer": {
                    "message_id": message_id,
                    "choice_id": choice_id,
                    "skill": part.get("skill"),
                    "skill_id": part.get("skill_id"),
                    "answers": answers,
                },
            }
        ],
        f"ch:{message_id}:{choice_id}",
        source_channel=source_channel,
    )
    await session.commit()
    if created:
        await publish_answer(conv.id, msg, reply)
    return reply
