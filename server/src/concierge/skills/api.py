"""REST API for skills: personal CRUD + admin-managed shared skills."""

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.dependencies import CurrentUser, SessionDep, require
from concierge.auth.models import User
from concierge.channels import ChannelEvent, get_hub
from concierge.conversations import service as conversations
from concierge.core import audit
from concierge.core.ratelimit import client_ip
from concierge.skills import service
from concierge.skills.format import SkillFormatError
from concierge.skills.models import Skill
from concierge.skills.service import SkillConflictError

log = logging.getLogger(__name__)

router = APIRouter()

ManageSkills = Annotated[User, Depends(require("manage_skills"))]
CreateSkills = Annotated[User, Depends(require("create_skills"))]

IMPORT_MAX = 5 * 1024 * 1024


class SkillOut(BaseModel):
    id: int
    name: str
    description: str
    aliases: list[str]
    triggers: list[str]
    argument_hint: str | None
    ephemeral: bool
    personal: bool
    enabled: bool
    version: int
    role_ids: list[Any]
    content: str
    files: list[str]
    created_at: datetime | None

    @classmethod
    def of(cls, s: Skill, user: User) -> "SkillOut":
        return cls(
            id=s.id,
            name=s.name,
            description=s.description,
            aliases=s.aliases,
            triggers=s.triggers,
            argument_hint=s.argument_hint,
            ephemeral=s.ephemeral,
            personal=s.owner_id == user.id,
            enabled=s.enabled,
            version=s.version,
            role_ids=s.role_ids,
            content=s.content,
            files=[f.path for f in s.files],
            created_at=s.created_at,
        )


class SkillBrief(BaseModel):
    id: int
    name: str
    aliases: list[str]
    description: str
    argument_hint: str | None
    personal: bool


class SkillIn(BaseModel):
    content: str = Field(min_length=1)
    role_ids: list[Any] | None = None
    enabled: bool | None = None


class SkillPatch(BaseModel):
    content: str | None = None
    role_ids: list[Any] | None = None
    enabled: bool | None = None


class FileIn(BaseModel):
    content: str


def _conflict(e: Exception) -> HTTPException:
    if isinstance(e, SkillConflictError):
        return HTTPException(409, str(e))
    return HTTPException(422, str(e))


async def _own_skill(session: AsyncSession, user: User, skill_id: int) -> Skill:
    skill = await session.get(Skill, skill_id)
    if skill is None or skill.owner_id != user.id:
        raise HTTPException(404, "skill not found")
    return skill


async def _shared_skill(session: AsyncSession, skill_id: int) -> Skill:
    skill = await session.get(Skill, skill_id)
    if skill is None or skill.owner_id is not None:
        raise HTTPException(404, "skill not found")
    return skill


def _export_response(skill: Skill) -> Response:
    return Response(
        service.export_archive(skill),
        media_type="application/zip",
        headers={
            "content-disposition": f'attachment; filename="{skill.name}.zip"'
        },
    )


async def _import(
    session: AsyncSession,
    owner_id: int | None,
    file: UploadFile,
    user: User,
    request: Request,
    role_ids: list[Any] | None,
) -> dict[str, Any]:
    data = await file.read(IMPORT_MAX + 1)
    if len(data) > IMPORT_MAX:
        raise HTTPException(413, "archive too large")
    try:
        skill, skipped = await service.import_archive(
            session,
            owner_id,
            data,
            file.filename or "skill.md",
            created_by=user.id,
            role_ids=role_ids,
        )
    except (SkillFormatError, SkillConflictError, UnicodeDecodeError) as e:
        raise _conflict(e) from e
    audit.record(
        session,
        "skill.create",
        actor_id=user.id,
        actor_name=user.username,
        target=f"skill:{skill.id}",
        detail={"name": skill.name, "import": file.filename},
        ip=client_ip(request),
    )
    await session.commit()
    return {"skill": SkillOut.of(skill, user).model_dump(), "skipped": skipped}


async def _put_file(
    session: AsyncSession, skill: Skill, path: str, body: FileIn
) -> dict[str, bool]:
    try:
        await service.put_file(session, skill, path, body.content)
    except SkillFormatError as e:
        raise HTTPException(422, str(e)) from e
    return {"ok": True}


async def _delete_file(session: AsyncSession, skill: Skill, path: str) -> dict[str, bool]:
    row = await service.get_file(session, skill, path)
    if row is None:
        raise HTTPException(404, "file not found")
    await service.delete_file(session, row)
    return {"ok": True}


# ---------- user-facing ----------


@router.get("/skills")
async def list_skills(
    user: CurrentUser, session: SessionDep
) -> list[SkillBrief]:
    skills = await service.available_skills(session, user)
    return [
        SkillBrief(
            id=s.id,
            name=s.name,
            aliases=s.aliases,
            description=s.description,
            argument_hint=s.argument_hint,
            personal=s.owner_id == user.id,
        )
        for s in skills
    ]


@router.get("/skills/mine")
async def list_mine(user: CreateSkills, session: SessionDep) -> list[SkillOut]:
    rows = await service.personal_skills(session, user.id)
    return [SkillOut.of(s, user) for s in rows]


@router.post("/skills/mine", status_code=201)
async def create_mine(
    body: SkillIn, user: CreateSkills, session: SessionDep, request: Request
) -> SkillOut:
    try:
        skill = await service.create_skill(
            session, user.id, body.content, created_by=user.id
        )
    except (SkillFormatError, SkillConflictError) as e:
        raise _conflict(e) from e
    audit.record(
        session,
        "skill.create",
        actor_id=user.id,
        actor_name=user.username,
        target=f"skill:{skill.id}",
        detail={"name": skill.name, "personal": True},
        ip=client_ip(request),
    )
    await session.commit()
    return SkillOut.of(skill, user)


@router.get("/skills/mine/{skill_id}")
async def get_mine(
    skill_id: int, user: CreateSkills, session: SessionDep
) -> SkillOut:
    return SkillOut.of(await _own_skill(session, user, skill_id), user)


@router.patch("/skills/mine/{skill_id}")
async def patch_mine(
    skill_id: int,
    body: SkillPatch,
    user: CreateSkills,
    session: SessionDep,
    request: Request,
) -> SkillOut:
    skill = await _own_skill(session, user, skill_id)
    try:
        skill = await service.update_skill(session, skill, content=body.content)
    except (SkillFormatError, SkillConflictError) as e:
        raise _conflict(e) from e
    audit.record(
        session,
        "skill.update",
        actor_id=user.id,
        actor_name=user.username,
        target=f"skill:{skill.id}",
        detail={"name": skill.name},
        ip=client_ip(request),
    )
    await session.commit()
    return SkillOut.of(skill, user)


@router.delete("/skills/mine/{skill_id}")
async def delete_mine(
    skill_id: int,
    user: CreateSkills,
    session: SessionDep,
    request: Request,
) -> dict[str, bool]:
    skill = await _own_skill(session, user, skill_id)
    audit.record(
        session,
        "skill.delete",
        actor_id=user.id,
        actor_name=user.username,
        target=f"skill:{skill.id}",
        detail={"name": skill.name},
        ip=client_ip(request),
    )
    await service.delete_skill(session, skill)
    return {"ok": True}


@router.post("/skills/mine/import", status_code=201)
async def import_mine(
    user: CreateSkills,
    session: SessionDep,
    request: Request,
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    return await _import(session, user.id, file, user, request, None)


@router.get("/skills/mine/{skill_id}/export")
async def export_mine(
    skill_id: int, user: CreateSkills, session: SessionDep
) -> Response:
    return _export_response(await _own_skill(session, user, skill_id))


@router.put("/skills/mine/{skill_id}/files/{path:path}")
async def put_mine_file(
    skill_id: int,
    path: str,
    body: FileIn,
    user: CreateSkills,
    session: SessionDep,
) -> dict[str, bool]:
    return await _put_file(
        session, await _own_skill(session, user, skill_id), path, body
    )


@router.delete("/skills/mine/{skill_id}/files/{path:path}")
async def delete_mine_file(
    skill_id: int,
    path: str,
    user: CreateSkills,
    session: SessionDep,
) -> dict[str, bool]:
    return await _delete_file(
        session, await _own_skill(session, user, skill_id), path
    )


# ---------- admin (shared skills) ----------


@router.get("/admin/skills")
async def admin_list(user: ManageSkills, session: SessionDep) -> list[SkillOut]:
    rows = await service.shared_skills(session)
    return [SkillOut.of(s, user) for s in rows]


@router.post("/admin/skills", status_code=201)
async def admin_create(
    body: SkillIn, user: ManageSkills, session: SessionDep, request: Request
) -> SkillOut:
    try:
        skill = await service.create_skill(
            session,
            None,
            body.content,
            created_by=user.id,
            role_ids=body.role_ids,
        )
    except (SkillFormatError, SkillConflictError) as e:
        raise _conflict(e) from e
    if body.enabled is not None:
        skill = await service.update_skill(session, skill, enabled=body.enabled)
    audit.record(
        session,
        "skill.create",
        actor_id=user.id,
        actor_name=user.username,
        target=f"skill:{skill.id}",
        detail={"name": skill.name},
        ip=client_ip(request),
    )
    await session.commit()
    return SkillOut.of(skill, user)


@router.get("/admin/skills/{skill_id}")
async def admin_get(
    skill_id: int, user: ManageSkills, session: SessionDep
) -> SkillOut:
    return SkillOut.of(await _shared_skill(session, skill_id), user)


@router.patch("/admin/skills/{skill_id}")
async def admin_patch(
    skill_id: int,
    body: SkillPatch,
    user: ManageSkills,
    session: SessionDep,
    request: Request,
) -> SkillOut:
    skill = await _shared_skill(session, skill_id)
    try:
        skill = await service.update_skill(
            session,
            skill,
            content=body.content,
            role_ids=body.role_ids,
            enabled=body.enabled,
        )
    except (SkillFormatError, SkillConflictError) as e:
        raise _conflict(e) from e
    audit.record(
        session,
        "skill.update",
        actor_id=user.id,
        actor_name=user.username,
        target=f"skill:{skill.id}",
        detail={"name": skill.name},
        ip=client_ip(request),
    )
    await session.commit()
    return SkillOut.of(skill, user)


@router.delete("/admin/skills/{skill_id}")
async def admin_delete(
    skill_id: int,
    user: ManageSkills,
    session: SessionDep,
    request: Request,
) -> dict[str, bool]:
    skill = await _shared_skill(session, skill_id)
    audit.record(
        session,
        "skill.delete",
        actor_id=user.id,
        actor_name=user.username,
        target=f"skill:{skill.id}",
        detail={"name": skill.name},
        ip=client_ip(request),
    )
    await service.delete_skill(session, skill)
    return {"ok": True}


@router.post("/admin/skills/import", status_code=201)
async def admin_import(
    user: ManageSkills,
    session: SessionDep,
    request: Request,
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    return await _import(session, None, file, user, request, None)


@router.get("/admin/skills/{skill_id}/export")
async def admin_export(
    skill_id: int, user: ManageSkills, session: SessionDep
) -> Response:
    return _export_response(await _shared_skill(session, skill_id))


@router.put("/admin/skills/{skill_id}/files/{path:path}")
async def admin_put_file(
    skill_id: int,
    path: str,
    body: FileIn,
    user: ManageSkills,
    session: SessionDep,
) -> dict[str, bool]:
    return await _put_file(
        session, await _shared_skill(session, skill_id), path, body
    )


@router.delete("/admin/skills/{skill_id}/files/{path:path}")
async def admin_delete_file(
    skill_id: int,
    path: str,
    user: ManageSkills,
    session: SessionDep,
) -> dict[str, bool]:
    return await _delete_file(
        session, await _shared_skill(session, skill_id), path
    )


# ---------- active skill on a conversation ----------


@router.delete("/conversations/{conversation_id}/skill")
async def clear_active_skill(
    conversation_id: int, user: CurrentUser, session: SessionDep
) -> dict[str, bool]:
    conv = await conversations.get_member_conversation(
        session, conversation_id, user.id
    )
    if conv is None:
        raise HTTPException(404, "conversation not found")
    msg = await service.deactivate(session, conv, user, "api")
    if msg is not None:
        from concierge.agent.runtime import _message_dict

        await get_hub().publish(
            ChannelEvent(
                "message.created",
                conv.id,
                {"message": _message_dict(msg)},
            )
        )
    return {"ok": True}
