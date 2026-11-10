from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth import security
from concierge.auth.models import User
from concierge.auth.permissions import has_permission
from concierge.core.db import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(request: Request, session: SessionDep) -> User:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    user_id = security.decode_access_token(header[7:].strip())
    user = await session.get(User, user_id) if user_id is not None else None
    if user is None or not user.is_active:
        raise HTTPException(401, "invalid or expired token")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require(permission: str) -> Callable[..., Coroutine[Any, Any, User]]:
    async def dep(user: CurrentUser) -> User:
        if not has_permission(user.role.permissions, permission):
            raise HTTPException(403, f"permission denied: {permission}")
        return user

    return dep
