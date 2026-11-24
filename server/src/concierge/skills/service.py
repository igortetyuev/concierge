"""Skills: visibility, CRUD, archives, activation state.

Active skill lives in `conversations.settings["active_skill"]`; its body is
re-read from the DB on every run so edits apply immediately. A shared skill
(owner_id NULL) is visible when enabled and its role_ids is empty, ["*"] or
contains the user's role; a personal skill shadows a shared one by name.
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.channels import ChannelEvent, get_hub
from concierge.conversations import service as conversations
from concierge.conversations.models import Conversation, Message
from concierge.core.db import utcnow
from concierge.skills import format as skill_format
from concierge.skills.format import (
    FILE_MAX,
    FILES_MAX,
    SkillDoc,
    SkillFormatError,
    parse_skill_md,
    valid_file_path,
)
from concierge.skills.models import Skill, SkillFile

log = logging.getLogger(__name__)

MAX_PERSONAL_SKILLS = 100
CATALOG_MAX = 50
CATALOG_DESC_MAX = 300


class SkillConflictError(ValueError):
    """Name/alias collision inside the skill's scope (HTTP 409)."""


def _tokens(skill: Skill) -> set[str]:
    return {skill.name, *skill.aliases}


async def _check_collisions(
    session: AsyncSession,
    doc: SkillDoc,
    owner_id: int | None,
    exclude_id: int | None = None,
) -> None:
    """No name/alias overlap with sibling skills in the same scope."""
    rows = (
        await session.execute(
            select(Skill).where(
                Skill.owner_id.is_(None)
                if owner_id is None
                else Skill.owner_id == owner_id
            )
        )
    ).scalars().all()
    mine = {doc.name, *doc.aliases}
    for other in rows:
        if exclude_id is not None and other.id == exclude_id:
            continue
        overlap = mine & _tokens(other)
        if overlap:
            raise SkillConflictError(
                f"конфликт имён со скиллом {other.name!r}: "  # noqa: RUF001
                f"{', '.join(sorted(overlap))}"
            )


async def available_skills(
    session: AsyncSession, user: User
) -> list[Skill]:
    """Personal + shared skills the user can activate; personal shadows."""
    rows = (
        await session.execute(
            select(Skill).where(Skill.enabled.is_(True)).order_by(Skill.name)
        )
    ).scalars().all()
    out: dict[str, Skill] = {}
    personal: dict[str, Skill] = {}
    for s in rows:
        if s.owner_id == user.id:
            personal[s.name] = s
        elif s.owner_id is None and (
            not s.role_ids or "*" in s.role_ids or user.role_id in s.role_ids
        ):
            out[s.name] = s
    out.update(personal)
    return list(out.values())


async def personal_skills(
    session: AsyncSession, user_id: int
) -> list[Skill]:
    rows = (
        await session.execute(
            select(Skill)
            .where(Skill.owner_id == user_id)
            .order_by(Skill.name)
        )
    ).scalars()
    return list(rows)


async def shared_skills(session: AsyncSession) -> list[Skill]:
    rows = (
        await session.execute(
            select(Skill)
            .where(Skill.owner_id.is_(None))
            .order_by(Skill.name)
        )
    ).scalars()
    return list(rows)


async def resolve(
    session: AsyncSession, user: User, token: str
) -> Skill | None:
    """Find an available skill by name or alias (case-insensitive)."""
    needle = token.strip().lower()
    if not needle:
        return None
    for skill in await available_skills(session, user):
        if needle in _tokens(skill):
            return skill
    return None


def _apply_doc(skill: Skill, doc: SkillDoc) -> None:
    skill.name = doc.name
    skill.description = doc.description
    skill.aliases = doc.aliases
    skill.triggers = doc.triggers
    skill.argument_hint = doc.argument_hint
    skill.content = doc.content


async def create_skill(
    session: AsyncSession,
    owner_id: int | None,
    content: str,
    *,
    created_by: int,
    role_ids: list[Any] | None = None,
    files: dict[str, str] | None = None,
) -> Skill:
    doc = parse_skill_md(content)
    if owner_id is not None:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Skill)
                .where(Skill.owner_id == owner_id)
            )
        ).scalar_one()
        if count >= MAX_PERSONAL_SKILLS:
            raise SkillFormatError(
                f"лимит {MAX_PERSONAL_SKILLS} личных скиллов"
            )
    await _check_collisions(session, doc, owner_id)
    skill = Skill(
        owner_id=owner_id,
        role_ids=role_ids or [],
        created_by=created_by,
    )
    _apply_doc(skill, doc)
    session.add(skill)
    await session.flush()
    for path, text in (files or {}).items():
        check_file(path, text)
        session.add(SkillFile(skill_id=skill.id, path=path, content=text))
    await session.commit()
    await session.refresh(skill)
    return skill


async def update_skill(
    session: AsyncSession,
    skill: Skill,
    *,
    content: str | None = None,
    role_ids: list[Any] | None = None,
    enabled: bool | None = None,
) -> Skill:
    if content is not None and content != skill.content:
        doc = parse_skill_md(content)
        await _check_collisions(
            session, doc, skill.owner_id, exclude_id=skill.id
        )
        _apply_doc(skill, doc)
        skill.version += 1
    if role_ids is not None and skill.owner_id is None:
        skill.role_ids = role_ids
    if enabled is not None:
        skill.enabled = enabled
    skill.updated_at = utcnow()
    await session.commit()
    await session.refresh(skill)
    return skill


async def delete_skill(session: AsyncSession, skill: Skill) -> None:
    await session.delete(skill)
    await session.commit()


def check_file(path: str, text: str) -> None:
    if not valid_file_path(path):
        raise SkillFormatError(f"недопустимый путь файла: {path!r}")
    if len(text.encode("utf-8")) > FILE_MAX:
        raise SkillFormatError(f"файл {path} больше 256 КБ")


async def put_file(
    session: AsyncSession, skill: Skill, path: str, content: str
) -> SkillFile:
    check_file(path, content)
    row = (
        await session.execute(
            select(SkillFile).where(
                SkillFile.skill_id == skill.id, SkillFile.path == path
            )
        )
    ).scalar_one_or_none()
    if row is None:
        count = (
            await session.execute(
                select(func.count())
                .select_from(SkillFile)
                .where(SkillFile.skill_id == skill.id)
            )
        ).scalar_one()
        if count >= FILES_MAX:
            raise SkillFormatError(f"лимит {FILES_MAX} файлов на скилл")
        row = SkillFile(skill_id=skill.id, path=path, content=content)
        session.add(row)
    else:
        row.content = content
    await session.commit()
    return row


async def get_file(
    session: AsyncSession, skill: Skill, path: str
) -> SkillFile | None:
    return (
        await session.execute(
            select(SkillFile).where(
                SkillFile.skill_id == skill.id, SkillFile.path == path
            )
        )
    ).scalar_one_or_none()


async def delete_file(session: AsyncSession, row: SkillFile) -> None:
    await session.delete(row)
    await session.commit()


def file_map(skill: Skill) -> dict[str, str]:
    return {f.path: f.content for f in skill.files}


async def import_archive(
    session: AsyncSession,
    owner_id: int | None,
    data: bytes,
    filename: str,
    *,
    created_by: int,
    role_ids: list[Any] | None = None,
) -> tuple[Skill, list[str]]:
    """Import .md/.zip -> (skill, skipped binary paths)."""
    content, files, skipped = skill_format.unpack_archive(data, filename)
    skill = await create_skill(
        session,
        owner_id,
        content,
        created_by=created_by,
        role_ids=role_ids,
        files=files,
    )
    return skill, skipped


def export_archive(skill: Skill) -> bytes:
    return skill_format.pack_archive(skill.name, skill.content, file_map(skill))


# ---------- activation ----------


async def _post_skill_event(
    session: AsyncSession,
    conv: Conversation,
    skill: Skill,
    action: str,
    by: str,
) -> Message:
    return await conversations.post_system_message(
        session,
        conv,
        [
            {
                "type": "skill_event",
                "action": action,
                "skill_id": skill.id,
                "name": skill.name,
                "by": by,
            }
        ],
    )


async def _publish_state(conv_id: int, active: dict[str, Any] | None) -> None:
    await get_hub().publish(
        ChannelEvent(
            "conversation.updated",
            conv_id,
            {"conversation": {"id": conv_id, "active_skill": active}},
        )
    )


async def activate(
    session: AsyncSession,
    conv: Conversation,
    skill: Skill,
    args: str,
    by_user: User,
) -> Message:
    """Pin the skill to the session and post a `skill_event` system part."""
    settings = dict(conv.settings or {})
    settings["active_skill"] = {
        "skill_id": skill.id,
        "name": skill.name,
        "args": args,
        "activated_by": by_user.id,
        "activated_at": datetime.now().isoformat(timespec="seconds"),
    }
    conv.settings = settings
    await session.commit()
    msg = await _post_skill_event(session, conv, skill, "start", by_user.display_name)
    await _publish_state(
        conv.id,
        {
            "skill_id": skill.id,
            "name": skill.name,
            "description": skill.description,
        },
    )
    return msg


async def deactivate(
    session: AsyncSession,
    conv: Conversation,
    by_user: User | None,
    reason: str,
    *,
    silent: bool = False,
) -> Message | None:
    """Drop the active skill; posts `skill_event` unless silent/none set."""
    state = (conv.settings or {}).get("active_skill")
    if not isinstance(state, dict):
        return None
    settings = dict(conv.settings)
    settings.pop("active_skill")
    conv.settings = settings
    await session.commit()
    msg = None
    if not silent:
        skill = await session.get(Skill, state.get("skill_id") or 0)
        fake = skill or Skill(name=str(state.get("name") or "?"))
        msg = await _post_skill_event(
            session,
            conv,
            fake,
            "end",
            by_user.display_name if by_user else reason,
        )
    await _publish_state(conv.id, None)
    return msg


async def active_skill_state(
    session: AsyncSession, conv: Conversation
) -> tuple[Skill, str] | None:
    """(skill, args) for the session; silently unsets a stale pointer."""
    state = (conv.settings or {}).get("active_skill")
    if not isinstance(state, dict):
        return None
    skill = await session.get(Skill, state.get("skill_id") or 0)
    if skill is None or not skill.enabled:
        settings = dict(conv.settings)
        settings.pop("active_skill", None)
        conv.settings = settings
        await session.commit()
        await _publish_state(conv.id, None)
        return None
    return skill, str(state.get("args") or "")


def catalog_block(skills: list[Skill]) -> str:
    """[Скиллы] prompt chunk: /name (aliases) — description; argument hint."""
    lines = []
    for s in skills[:CATALOG_MAX]:
        aliases = f" (также: {', '.join('/' + a for a in s.aliases)})" if s.aliases else ""
        hint = f" [аргументы: {s.argument_hint}]" if s.argument_hint else ""
        desc = s.description[:CATALOG_DESC_MAX]
        lines.append(f"/{s.name}{aliases} — {desc}{hint}")
    return "\n".join(lines)
