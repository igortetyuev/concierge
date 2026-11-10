from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from concierge.auth.dependencies import CurrentUser, SessionDep
from concierge.memory import service
from concierge.memory.models import MemoryItem

router = APIRouter()


class MemoryItemOut(BaseModel):
    id: int
    scope: str
    owner_id: int | None
    text: str
    kind: str
    source_message_id: int | None
    created_at: datetime | None

    @classmethod
    def of(cls, item: MemoryItem) -> "MemoryItemOut":
        return cls(
            id=item.id,
            scope=item.scope,
            owner_id=item.owner_id,
            text=item.text,
            kind=item.kind,
            source_message_id=item.source_message_id,
            created_at=item.created_at,
        )


@router.get("/memory")
async def list_memory(
    user: CurrentUser, session: SessionDep
) -> list[MemoryItemOut]:
    items = await service.list_items(session, user)
    return [MemoryItemOut.of(i) for i in items]


class PostMemoryBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    scope: Literal["user", "family"] = "user"


@router.post("/memory", status_code=201)
async def post_memory(
    body: PostMemoryBody, user: CurrentUser, session: SessionDep
) -> MemoryItemOut:
    try:
        item = await service.remember(
            session, user, body.text.strip(), body.scope
        )
    except PermissionError:
        raise HTTPException(403, "no permission to write family memory") from None
    return MemoryItemOut.of(item)


@router.delete("/memory/{memory_id}")
async def delete_memory(
    memory_id: int, user: CurrentUser, session: SessionDep
) -> dict[str, bool]:
    try:
        ok = await service.forget(session, user, memory_id)
    except PermissionError:
        raise HTTPException(403, "cannot delete this memory item") from None
    if not ok:
        raise HTTPException(404, "memory item not found")
    return {"ok": True}
