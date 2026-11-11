import secrets
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete

from concierge.auth.dependencies import SessionDep, require
from concierge.auth.models import User
from concierge.channels.telegram import gateway as tg_gateway
from concierge.channels.telegram import service as tg_service
from concierge.channels.telegram.models import (
    TelegramLink,
    TelegramLinkCode,
)
from concierge.channels.telegram.service import (
    LINK_CODE_TTL_SECONDS,
    hash_link_code,
)
from concierge.core import audit
from concierge.core.db import utcnow
from concierge.core.ratelimit import (
    LINK_CODE_LIMIT,
    LINK_CODE_WINDOW,
    client_ip,
    get_limiter,
)
from concierge.settings import service as settings_service

router = APIRouter(prefix="/telegram")

TelegramUser = Annotated[User, Depends(require("telegram"))]


class LinkCodeOut(BaseModel):
    code: str
    bot_username: str | None
    expires_at: datetime


@router.post("/link-code")
async def create_link_code(
    user: TelegramUser, session: SessionDep
) -> LinkCodeOut:
    if not get_limiter().allow(
        f"link_code:{user.id}", LINK_CODE_LIMIT, LINK_CODE_WINDOW
    ):
        raise HTTPException(429, "too many link codes, try later")
    await session.execute(
        delete(TelegramLinkCode).where(TelegramLinkCode.user_id == user.id)
    )
    code = secrets.token_hex(4)
    expires_at = utcnow() + timedelta(seconds=LINK_CODE_TTL_SECONDS)
    session.add(
        TelegramLinkCode(
            code_hash=hash_link_code(code),
            user_id=user.id,
            expires_at=expires_at,
        )
    )
    await session.commit()
    bot_username = await settings_service.get_global(
        session, "telegram.bot_username"
    )
    return LinkCodeOut(
        code=code,
        bot_username=str(bot_username) if bot_username else None,
        expires_at=expires_at,
    )


class LinkOut(BaseModel):
    linked: bool
    telegram_username: str | None = None
    telegram_user_id: int | None = None
    bot_username: str | None = None


@router.get("/link")
async def get_link(user: TelegramUser, session: SessionDep) -> LinkOut:
    link = await session.get(TelegramLink, user.id)
    bot_username = await settings_service.get_global(
        session, "telegram.bot_username"
    )
    return LinkOut(
        linked=link is not None,
        telegram_username=link.telegram_username if link else None,
        telegram_user_id=link.telegram_user_id if link else None,
        bot_username=str(bot_username) if bot_username else None,
    )


@router.delete("/link")
async def delete_link(
    user: TelegramUser, request: Request, session: SessionDep
) -> dict[str, bool]:
    if await session.get(TelegramLink, user.id) is None:
        raise HTTPException(404, "not linked")
    audit.record(
        session,
        "telegram.unlink",
        actor_id=user.id,
        actor_name=user.username,
        ip=client_ip(request),
    )
    dropped = await tg_service.unlink_user(session, user.id)
    for chat_id, thread_id in dropped:
        await tg_gateway.binding_dropped(chat_id, thread_id)
    return {"ok": True}
