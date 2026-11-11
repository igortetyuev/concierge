from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.agent.runtime import get_runtime
from concierge.auth.dependencies import CurrentUser, SessionDep, require
from concierge.auth.models import User
from concierge.auth.permissions import has_permission
from concierge.channels import ChannelEvent, get_hub
from concierge.conversations import service
from concierge.conversations.models import Conversation, ConversationMember, Message
from concierge.media import service as media_service
from concierge.media.models import Attachment
from concierge.memory import indexer as memory_indexer
from concierge.settings import service as settings_service

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


async def _bind_attachments(
    session: AsyncSession, parts: list[dict[str, Any]], conv_id: int, user: User
) -> None:
    """Adopt the uploader's unbound attachments referenced by a message."""
    changed = False
    for p in parts:
        aid = media_service.part_attachment_id(p)
        if aid is None:
            continue
        att = await session.get(Attachment, aid)
        if att is not None and att.conversation_id is None and att.uploaded_by == user.id:
            att.conversation_id = conv_id
            changed = True
    if changed:
        await session.commit()


async def _messages_out(
    session: AsyncSession, rows: list[Message]
) -> list[MessageOut]:
    expanded = await media_service.expand_messages(session, rows)
    out = []
    for m in rows:
        o = MessageOut.of(m)
        o.parts = expanded[m.id]
        out.append(o)
    return out


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
    if created:
        await _bind_attachments(session, body.parts, conv.id, user)
    out = (await _messages_out(session, [msg]))[0]
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
    return await _messages_out(session, rows)


VOICE_QUICK_MIN_CHARS_DEFAULT = 2
VOICE_QUICK_TITLE_MAX = 48


class VoiceQuickOut(BaseModel):
    status: Literal["ok", "empty_transcript", "stt_failed"]
    conversation_id: int | None = None
    message_id: int | None = None
    transcript: str | None = None


async def _fanout_new(
    session: AsyncSession, conv: Conversation, user: User, msgs: list[Message]
) -> None:
    """Push a freshly created session + its messages to the owner's devices.

    Recipients aren't subscribed to the new conversation yet, so events go by
    user, not by subscription.
    """
    hub = get_hub()
    conv_out = await _conv_out(session, conv)
    await hub.publish_users(
        {user.id},
        ChannelEvent(
            "conversation.updated",
            conv.id,
            {"conversation": conv_out.model_dump(mode="json")},
        ),
    )
    for out in await _messages_out(session, msgs):
        await hub.publish_users(
            {user.id},
            ChannelEvent(
                "message.created", conv.id, {"message": out.model_dump(mode="json")}
            ),
        )


@router.post("/conversations/voice-quick")
async def voice_quick(
    user: Annotated[User, Depends(require("voice_quick_session"))],
    session: SessionDep,
    file: Annotated[UploadFile | None, File()] = None,
    attachment_id: Annotated[int | None, Form()] = None,
) -> VoiceQuickOut:
    """One-tap voice session: audio -> STT -> new personal session + run."""
    if (file is None) == (attachment_id is None):
        raise HTTPException(400, "provide either file or attachment_id")

    existing: Attachment | None = None
    upload: tuple[str, str, bytes] | None = None
    if attachment_id is not None:
        existing = await session.get(Attachment, attachment_id)
        if existing is None or existing.uploaded_by != user.id:
            raise HTTPException(404, "attachment not found")
        if media_service.kind_of(existing.mime, existing.original_name) != "audio":
            raise HTTPException(400, "attachment is not audio")
    else:
        assert file is not None
        max_bytes = media_service.max_upload_bytes(user)
        if max_bytes <= 0:
            raise HTTPException(403, "uploads are not allowed for this role")
        data = await file.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise HTTPException(
                413,
                f"file exceeds the {max_bytes // (1024 * 1024)} MB upload limit",
            )
        if not data:
            raise HTTPException(400, "empty file")
        name = file.filename or "voice"
        mime = file.content_type or "application/octet-stream"
        if media_service.kind_of(mime, name) != "audio":
            raise HTTPException(400, "voice-quick accepts audio only")
        upload = (name, mime, data)

    conv = await service.create_conversation(
        session, user, "personal", "", [], origin="voice_quick"
    )
    assert conv is not None
    if existing is not None:
        att = existing
        await media_service.bind_to_conversation(session, att, conv.id)
    else:
        assert upload is not None
        att, _ = await media_service.create_attachment(
            session, user, upload[0], upload[1], upload[2], conv.id
        )

    transcript = (await media_service.transcribe(att.id)).strip()
    await session.refresh(att)

    if att.derived.get("status") != "done":
        # STT unavailable/failed: keep the session with the raw audio and a
        # system notice; the client can retry recognition in place.
        parts = [
            {
                "type": "audio",
                "attachment_id": att.id,
                "name": att.original_name,
                "mime": att.mime,
            }
        ]
        msg, _ = await service.post_message(session, conv, user, parts, None)
        notice = await service.post_system_message(
            session,
            conv,
            [
                {
                    "type": "text",
                    "text": "Ошибка распознавания речи. Нажмите «Повторить» "
                    "под записью.",
                }
            ],
        )
        await _fanout_new(session, conv, user, [msg, notice])
        return VoiceQuickOut(
            status="stt_failed", conversation_id=conv.id, message_id=msg.id
        )

    min_chars = int(
        await settings_service.get_value(
            session,
            user,
            "voice_quick.min_transcript_chars",
            VOICE_QUICK_MIN_CHARS_DEFAULT,
        )
        or VOICE_QUICK_MIN_CHARS_DEFAULT
    )
    if len(transcript) < min_chars:
        att.conversation_id = None
        await session.delete(conv)
        await session.commit()
        return VoiceQuickOut(
            status="empty_transcript", transcript=transcript or None
        )

    parts = [
        {"type": "text", "text": transcript, "transcribed": True},
        {
            "type": "audio",
            "attachment_id": att.id,
            "name": att.original_name,
            "mime": att.mime,
        },
    ]
    msg, _ = await service.post_message(session, conv, user, parts, None)
    if len(transcript) <= VOICE_QUICK_TITLE_MAX:
        # Short request: the transcript itself is the title; longer ones get
        # a chat_fast-generated title after the run (runtime._ensure_title).
        conv = await service.update_conversation(
            session, conv, title=transcript.split("\n", 1)[0].strip()
        )
    await _fanout_new(session, conv, user, [msg])
    memory_indexer.kick()
    get_runtime().schedule(conv.id, msg.id)
    return VoiceQuickOut(
        status="ok",
        conversation_id=conv.id,
        message_id=msg.id,
        transcript=transcript,
    )


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
    return await _messages_out(session, rows)
