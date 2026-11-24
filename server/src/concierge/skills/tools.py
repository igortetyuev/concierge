"""Agent tools for skills: use/end/read files/save personal skills."""

import logging
from collections.abc import Callable
from typing import Any, Literal

from pydantic_ai import RunContext
from sqlalchemy import select

from concierge.agent.tools import ToolDeps
from concierge.auth.models import User
from concierge.channels import ChannelEvent, get_hub
from concierge.conversations.models import Conversation
from concierge.core.db import get_sessionmaker
from concierge.skills import service
from concierge.skills.activation import SKILL_COMPAT_PREAMBLE
from concierge.skills.format import (
    SkillFormatError,
    body_of,
    parse_skill_md,
    render_body,
)
from concierge.skills.models import Skill
from concierge.skills.service import SkillConflictError

log = logging.getLogger(__name__)

READ_MAX = 20_000


async def _publish_event(conv_id: int, msg: Any) -> None:
    # Late import: agent.runtime imports this module for the skill tools.
    from concierge.agent.runtime import _message_dict

    await get_hub().publish(
        ChannelEvent(
            "message.created", conv_id, {"message": _message_dict(msg)}
        )
    )


def make_use_skill_tool(names: list[str]) -> Callable[..., Any]:
    """use_skill with `name` restricted to the skills available this run."""
    name_enum = Literal[tuple(names)]  # type: ignore[valid-type]

    async def use_skill(
        ctx: RunContext[ToolDeps], name: str, arguments: str = ""
    ) -> dict[str, Any]:
        """Activate a skill in this session (a /command or trigger phrase).

        The skill's instructions apply to this and following replies until
        `end_skill`. Call with the name from the [Скиллы] catalog.
        """
        async with get_sessionmaker()() as session:
            conv = await session.get(Conversation, ctx.deps.conversation_id)
            user = await session.get(User, ctx.deps.user_id)
            if conv is None or user is None:
                return {"error": "session not found"}
            skill = await service.resolve(session, user, name)
            if skill is None:
                return {
                    "error": f"unknown skill {name!r}",
                    "available": names,
                }
            body = render_body(
                body_of(skill.content), arguments, skill.name
            )
            msg = await service.activate(
                session, conv, skill, arguments, user
            )
        await _publish_event(ctx.deps.conversation_id, msg)
        return {
            "ok": True,
            "skill": skill.name,
            "instructions": f"{SKILL_COMPAT_PREAMBLE}\n\n{body}",
        }

    use_skill.__annotations__["name"] = name_enum
    return use_skill


async def end_skill(ctx: RunContext[ToolDeps], summary: str = "") -> dict[str, Any]:
    """Deactivate the skill currently active in this session.

    Use when the skill's scenario is finished. `summary` is a one-line
    outcome note for yourself; the session returns to normal behavior.
    """
    async with get_sessionmaker()() as session:
        conv = await session.get(Conversation, ctx.deps.conversation_id)
        user = await session.get(User, ctx.deps.user_id)
        if conv is None or user is None:
            return {"error": "session not found"}
        msg = await service.deactivate(session, conv, user, "end_skill")
    if msg is not None:
        await _publish_event(ctx.deps.conversation_id, msg)
    return {"ok": True}


async def read_skill_file(
    ctx: RunContext[ToolDeps], path: str, offset: int = 0
) -> dict[str, Any]:
    """Read a text file attached to the active skill (references/* etc).

    Returns up to 20 000 characters starting at `offset`.
    """
    async with get_sessionmaker()() as session:
        conv = await session.get(Conversation, ctx.deps.conversation_id)
        if conv is None:
            return {"error": "session not found"}
        state = await service.active_skill_state(session, conv)
        if state is None:
            return {"error": "no active skill"}
        skill, _args = state
        row = await service.get_file(session, skill, path.strip())
    if row is None:
        return {"error": f"no such file in skill: {path!r}"}
    text = row.content
    start = max(0, int(offset))
    return {
        "path": row.path,
        "offset": start,
        "total": len(text),
        "content": text[start : start + READ_MAX],
    }


async def save_skill(
    ctx: RunContext[ToolDeps],
    content: str,
    files: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create or update one of YOUR personal skills.

    `content` is the full SKILL.md (YAML frontmatter + Markdown body); use it
    when the user asks to save a procedure/prompt as a skill. `files` are
    optional extra text files (relative paths -> contents).
    """
    async with get_sessionmaker()() as session:
        user = await session.get(User, ctx.deps.user_id)
        if user is None:
            return {"error": "user not found"}
        try:
            doc_name = parse_skill_md(content).name
        except SkillFormatError as e:
            return {"error": str(e)}
        existing = (
            await session.execute(
                select(Skill).where(
                    Skill.owner_id == user.id,
                    Skill.name == doc_name,
                )
            )
        ).scalar_one_or_none()
        try:
            if existing is None:
                skill = await service.create_skill(
                    session,
                    user.id,
                    content,
                    created_by=user.id,
                    files=files,
                )
            else:
                skill = await service.update_skill(
                    session, existing, content=content
                )
                for path, text in (files or {}).items():
                    await service.put_file(session, skill, path, text)
        except (SkillFormatError, SkillConflictError) as e:
            return {"error": str(e)}
    return {"ok": True, "skill": skill.name, "id": skill.id}
