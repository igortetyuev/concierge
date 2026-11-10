from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from concierge.auth.dependencies import CurrentUser, SessionDep
from concierge.auth.models import User

router = APIRouter()


class UserRefOut(BaseModel):
    id: int
    username: str
    display_name: str


@router.get("/users")
async def list_users(_: CurrentUser, session: SessionDep) -> list[UserRefOut]:
    """Directory of active users for pickers (group creation, member add)."""
    rows = (
        await session.execute(
            select(User).where(User.is_active).order_by(User.display_name)
        )
    ).scalars()
    return [
        UserRefOut(id=u.id, username=u.username, display_name=u.display_name)
        for u in rows
    ]
