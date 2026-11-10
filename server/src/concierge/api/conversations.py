from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.agent.runtime import get_runtime
from concierge.auth.dependencies import CurrentUser, SessionDep
from concierge.auth.models import User
from concierge.auth.permissions import has_permission
from concierge.channels import ChannelEvent, get_hub
from concierge.conversations import service
from concierge.conversations.models import Conversation, ConversationMember, Message
from concierge.memory import indexer as memory_indexer

router = APIRouter()


class MemberOut(BaseModel):
    user_id: int
    username: str
    display_name: str
    joined_at: datetime | None
    last_read_seq: int


class ConversationOut(BaseModel):
    id: int
    kind: str
    title: str
    owner_id: int
    status: str
    reply_mode: str
    origin: str
    summary: str | None
    created_at: datetime | None
    updated_at: datetime | None
    archived_at: datetime | None
    members: list[MemberOut]


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    seq: int
    author_kind: str
    author_id: int | None
    parts: list[dict[str, Any]]
    run_id: int | None
    source_channel: str
    client_msg_id: str | None
    created_at: datetime | None

    @classmethod
    def of(cls, m: Message) -> "MessageOut":
        return cls(
            id=m.id,
            conversation_id=m.conversation_id,
            seq=m.seq,
            author_kind=m.author_kind,
            author_id=m.author_id,
            parts=m.parts,
            run_id=m.run_id,
            source_channel=m.source_channel,
            client_msg_id=m.client_msg_id,
            created_at=m.created_at,
        )


async def _members_out(session: AsyncSession, conv: Conversation) -> list[MemberOut]:
    rows = (
        await session.execute(
            select(ConversationMember, User)
            .join(User, User.id == ConversationMember.user_id)
            .where(ConversationMember.conversation_id == conv.id)
        )
    ).all()
    return [
        MemberOut(
            user_id=m.user_id,
            username=u.username,
            display_name=u.display_name,
            joined_at=m.joined_at,
            last_read_seq=m.last_read_seq,
        )
        for m, u in rows
    ]


async def _conv_out(session: AsyncSession, conv: Conversation) -> ConversationOut:
    return ConversationOut(
        id=conv.id,
        kind=conv.kind,
        title=conv.title,
        owner_id=conv.owner_id,
        status=conv.status,
        reply_mode=conv.reply_mode,
        origin=conv.origin,
        summary=conv.summary,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        archived_at=conv.archived_at,
        members=await _members_out(session, conv),
    )


async def _member_conv_or_404(
    session: AsyncSession, conversation_id: int, user: User
) -> Conversation:
    conv = await service.get_member_conversation(session, conversation_id, user.id)
    if conv is None:
        raise HTTPException(404, "conversation not found")
    return conv


def _can_manage(conv: Conversation, user: User) -> bool:
    return conv.owner_id == user.id or has_permission(
        user.role.permissions, "*"
    )


class CreateConversationBody(BaseModel):
    kind: Literal["personal", "group"]
    title: str = Field(default="", max_length=256)
    member_ids: list[int] = []


@router.post("/conversations", status_code=201)
async def create_conversation(
    body: CreateConversationBody, user: CurrentUser, session: SessionDep
) -> ConversationOut:
    conv = await service.create_conversation(
        session, user, body.kind, body.title, body.member_ids
    )
    if conv is None:
        raise HTTPException(400, "unknown member user ids")
    return await _conv_out(session, conv)


@router.get("/conversations")
async def list_conversations(
    user: CurrentUser,
    session: SessionDep,
    status: Annotated[str, Query(pattern="^(active|archived|all)$")] = "active",
) -> list[ConversationOut]:
    convs = await service.list_conversations(session, user.id, status)
    return [await _conv_out(session, c) for c in convs]


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: int, user: CurrentUser, session: SessionDep
) -> ConversationOut:
    conv = await _member_conv_or_404(session, conversation_id, user)
    return await _conv_out(session, conv)


class PatchConversationBody(BaseModel):
    title: str | None = Field(default=None, max_length=256)
    reply_mode: Literal["always", "mention"] | None = None
    status: Literal["active", "archived"] | None = None


@router.patch("/conversations/{conversation_id}")
async def patch_conversation(
    conversation_id: int,
    body: PatchConversationBody,
    user: CurrentUser,
    session: SessionDep,
) -> ConversationOut:
    conv = await _member_conv_or_404(session, conversation_id, user)
    if not _can_manage(conv, user):
        raise HTTPException(403, "only the owner can modify the conversation")
    archive = body.status == "archived" and conv.status != "archived"
    conv = await service.update_conversation(
        session,
        conv,
        title=body.title,
        reply_mode=body.reply_mode,
        status=body.status,
    )
    if archive:
        memory_indexer.schedule_archive(conv.id)
    out = await _conv_out(session, conv)
    await get_hub().publish(
        ChannelEvent(
            "conversation.updated",
            conv.id,
            {"conversation": out.model_dump(mode="json")},
        )
    )
    return out


class AddMembersBody(BaseModel):
    user_ids: list[int]


@router.post("/conversations/{conversation_id}/members")
async def add_members(
    conversation_id: int,
    body: AddMembersBody,
    user: CurrentUser,
    session: SessionDep,
) -> ConversationOut:
    conv = await _member_conv_or_404(session, conversation_id, user)
    if not _can_manage(conv, user):
        raise HTTPException(403, "only the owner can add members")
    added = await service.add_members(session, conv, body.user_ids)
    if added is None:
        raise HTTPException(400, "unknown user ids")
    out = await _conv_out(session, conv)
    event = ChannelEvent(
        "conversation.updated",
        conv.id,
        {"conversation": out.model_dump(mode="json")},
    )
    hub = get_hub()
    await hub.publish(event)
    await hub.publish_users(set(added), event)
    return out


@router.delete("/conversations/{conversation_id}/members/{user_id}")
async def remove_member(
    conversation_id: int, user_id: int, user: CurrentUser, session: SessionDep
) -> ConversationOut:
    conv = await _member_conv_or_404(session, conversation_id, user)
    if user_id != user.id and not _can_manage(conv, user):
        raise HTTPException(403, "only the owner can remove members")
    if user_id == conv.owner_id:
        raise HTTPException(400, "cannot remove the owner")
    if not await service.remove_member(session, conv, user_id):
        raise HTTPException(404, "member not found")
    out = await _conv_out(session, conv)
    event = ChannelEvent(
        "conversation.updated",
        conv.id,
        {"conversation": out.model_dump(mode="json")},
    )
    hub = get_hub()
    await hub.publish(event)
    await hub.publish_users({user_id}, event)
    return out


@router.get("/conversations/{conversation_id}/members")
async def list_members(
    conversation_id: int, user: CurrentUser, session: SessionDep
) -> list[MemberOut]:
    conv = await _member_conv_or_404(session, conversation_id, user)
    return await _members_out(session, conv)


class PostMessageBody(BaseModel):
    parts: list[dict[str, Any]] = Field(min_length=1)
    client_msg_id: str | None = Field(default=None, max_length=64)


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def post_message(
    conversation_id: int,
    body: PostMessageBody,
    response: Response,
    user: CurrentUser,
    session: SessionDep,
) -> MessageOut:
    conv = await _member_conv_or_404(session, conversation_id, user)
    if conv.status != "active":
        raise HTTPException(409, "conversation is archived")
    msg, created = await service.post_message(
        session, conv, user, body.parts, body.client_msg_id
    )
    out = MessageOut.of(msg)
    if created:
        await get_hub().publish(
            ChannelEvent(
                "message.created", conv.id, {"message": out.model_dump(mode="json")}
            )
        )
        memory_indexer.kick()
        get_runtime().schedule(conv.id, msg.id)
    else:
        response.status_code = 200
    return out


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: int,
    user: CurrentUser,
    session: SessionDep,
    before_seq: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[MessageOut]:
    conv = await _member_conv_or_404(session, conversation_id, user)
    rows = await service.list_messages(session, conv.id, before_seq, limit)
    return [MessageOut.of(m) for m in rows]


class MarkReadBody(BaseModel):
    seq: int = Field(ge=0)


@router.post("/conversations/{conversation_id}/read")
async def mark_read(
    conversation_id: int,
    body: MarkReadBody,
    user: CurrentUser,
    session: SessionDep,
) -> dict[str, int]:
    await _member_conv_or_404(session, conversation_id, user)
    last_read = await service.mark_read(
        session, conversation_id, user.id, body.seq
    )
    if last_read is None:
        raise HTTPException(404, "member not found")
    await get_hub().publish(
        ChannelEvent(
            "read.updated",
            conversation_id,
            {"user_id": user.id, "last_read_seq": last_read},
        )
    )
    return {"last_read_seq": last_read}


@router.get("/search")
async def search(
    user: CurrentUser,
    session: SessionDep,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[MessageOut]:
    rows = await service.search_messages(session, user.id, q, limit)
    return [MessageOut.of(m) for m in rows]
