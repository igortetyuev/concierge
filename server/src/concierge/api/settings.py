from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from concierge.auth.dependencies import CurrentUser, SessionDep
from concierge.auth.models import User
from concierge.auth.permissions import settings_allowed
from concierge.core import audit
from concierge.core.ratelimit import client_ip
from concierge.settings import service
from concierge.settings.models import SettingScope

router = APIRouter()

ScopeParam = Literal["global", "role", "user"]

_SCOPE_ENUM = {
    "global": SettingScope.global_,
    "role": SettingScope.role,
    "user": SettingScope.user,
}


def _scope_id(scope: ScopeParam, user: User) -> int:
    if scope == "global":
        return 0
    if scope == "role":
        return user.role_id
    return user.id


@router.get("/settings/{scope}")
async def get_scope(
    scope: ScopeParam, user: CurrentUser, session: SessionDep
) -> dict[str, Any]:
    if not settings_allowed(user.role.permissions, scope, write=False):
        raise HTTPException(403, f"no read access to {scope} settings")
    return await service.list_scope(session, _SCOPE_ENUM[scope], _scope_id(scope, user))


class SettingBody(BaseModel):
    key: str
    value: Any


@router.put("/settings/{scope}")
async def put_scope(
    scope: ScopeParam,
    body: SettingBody,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> dict[str, bool]:
    if not settings_allowed(user.role.permissions, scope, write=True):
        raise HTTPException(403, f"no write access to {scope} settings")
    audit.record(
        session,
        "settings.put",
        actor_id=user.id,
        actor_name=user.username,
        target=f"{scope}:{body.key}",
        ip=client_ip(request),
    )
    await service.set_value(
        session, _SCOPE_ENUM[scope], _scope_id(scope, user), body.key, body.value, user.id
    )
    return {"ok": True}


@router.delete("/settings/{scope}/{key}")
async def delete_key(
    scope: ScopeParam,
    key: str,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> dict[str, bool]:
    if not settings_allowed(user.role.permissions, scope, write=True):
        raise HTTPException(403, f"no write access to {scope} settings")
    audit.record(
        session,
        "settings.delete",
        actor_id=user.id,
        actor_name=user.username,
        target=f"{scope}:{key}",
        ip=client_ip(request),
    )
    await service.delete_value(session, _SCOPE_ENUM[scope], _scope_id(scope, user), key)
    return {"ok": True}
