from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from concierge.auth import security, service
from concierge.auth.dependencies import CurrentUser, SessionDep, require
from concierge.auth.models import Device, Invite, User
from concierge.core.config import get_settings
from concierge.core.db import utcnow

router = APIRouter()

REFRESH_COOKIE = "concierge_refresh"


def _set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
        max_age=settings.refresh_token_ttl_days * 86400,
    )


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    permissions: dict[str, Any]

    @classmethod
    def of(cls, user: User) -> "UserOut":
        return cls(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role.name,
            permissions=user.role.permissions,
        )


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


def _token_out(user: User) -> TokenOut:
    token, ttl = security.create_access_token(user.id)
    return TokenOut(access_token=token, expires_in=ttl, user=UserOut.of(user))


class LoginBody(BaseModel):
    username: str
    password: str
    device_name: str = ""


@router.post("/auth/login")
async def login(body: LoginBody, response: Response, session: SessionDep) -> TokenOut:
    user = await service.authenticate(session, body.username, body.password)
    if user is None:
        raise HTTPException(401, "invalid credentials")
    _device, refresh_token = await service.create_device(
        session, user, body.device_name
    )
    _set_refresh_cookie(response, refresh_token)
    return _token_out(user)


@router.post("/auth/refresh")
async def refresh(request: Request, response: Response, session: SessionDep) -> TokenOut:
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise HTTPException(401, "no refresh token")
    result = await service.rotate_refresh_token(session, token)
    if result is None:
        raise HTTPException(401, "invalid or expired refresh token")
    user, _device, new_token = result
    _set_refresh_cookie(response, new_token)
    return _token_out(user)


@router.post("/auth/logout")
async def logout(request: Request, response: Response, session: SessionDep) -> dict[str, bool]:
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        await service.revoke_refresh_token(session, token)
    response.delete_cookie(REFRESH_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.of(user)


class DeviceOut(BaseModel):
    id: int
    name: str
    last_seen_at: datetime | None
    created_at: datetime | None
    revoked_at: datetime | None


@router.get("/auth/devices")
async def list_devices(user: CurrentUser, session: SessionDep) -> list[DeviceOut]:
    rows = (
        await session.execute(
            select(Device)
            .where(Device.user_id == user.id)
            .order_by(Device.last_seen_at.desc())
        )
    ).scalars()
    return [
        DeviceOut(
            id=d.id,
            name=d.name,
            last_seen_at=d.last_seen_at,
            created_at=d.created_at,
            revoked_at=d.revoked_at,
        )
        for d in rows
    ]


@router.delete("/auth/devices/{device_id}")
async def revoke_device(
    device_id: int, user: CurrentUser, session: SessionDep
) -> dict[str, bool]:
    device = await session.get(Device, device_id)
    if device is None or device.user_id != user.id:
        raise HTTPException(404, "device not found")
    if device.revoked_at is None:
        device.revoked_at = utcnow()
        await session.commit()
    return {"ok": True}


class InviteBody(BaseModel):
    role_id: int


class InviteOut(BaseModel):
    id: int
    token: str
    role_id: int
    expires_at: datetime


@router.post("/invites", dependencies=[Depends(require("create_invites"))])
async def create_invite(
    body: InviteBody, user: CurrentUser, session: SessionDep
) -> InviteOut:
    result = await service.create_invite(session, body.role_id, user.id)
    if result is None:
        raise HTTPException(404, "role not found")
    invite, token = result
    return InviteOut(
        id=invite.id, token=token, role_id=invite.role_id, expires_at=invite.expires_at
    )


class RegisterBody(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=256)
    device_name: str = ""


@router.post("/auth/register")
async def register(
    body: RegisterBody,
    response: Response,
    session: SessionDep,
    token: Annotated[str, Query()],
) -> TokenOut:
    user = await service.register_with_invite(
        session, token, body.username, body.display_name, body.password
    )
    if user is None:
        raise HTTPException(400, "invalid, expired or used invite token")
    _device, refresh_token = await service.create_device(
        session, user, body.device_name
    )
    _set_refresh_cookie(response, refresh_token)
    return _token_out(user)


class InviteListOut(BaseModel):
    id: int
    role_id: int
    expires_at: datetime
    used_by: int | None


@router.get("/invites", dependencies=[Depends(require("create_invites"))])
async def list_invites(session: SessionDep) -> list[InviteListOut]:
    rows = (await session.execute(select(Invite).order_by(Invite.id))).scalars()
    return [
        InviteListOut(
            id=i.id, role_id=i.role_id, expires_at=i.expires_at, used_by=i.used_by
        )
        for i in rows
    ]
