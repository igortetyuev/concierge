from typing import Any

from sqlalchemy import String, cast, func, literal_column, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.channels.telegram.models import TelegramBinding
from concierge.conversations.models import (
    Conversation,
    ConversationMember,
    Message,
    Run,
)
from concierge.core.db import utcnow
from concierge.memory.models import MemoryItem


async def create_conversation(
    session: AsyncSession,
    owner: User,
    kind: str,
    title: str,
    member_ids: list[int],
    origin: str = "web",
) -> Conversation | None:
    ids = {owner.id, *member_ids} if kind == "group" else {owner.id}
    found = set(
        (await session.execute(select(User.id).where(User.id.in_(ids)))).scalars()
    )
    if found != ids:
        return None
    conv = Conversation(
        kind=kind,
        title=title,
        owner_id=owner.id,
        origin=origin,
        reply_mode="mention" if kind == "group" else "always",
    )
    session.add(conv)
    await session.flush()
    for uid in ids:
        session.add(ConversationMember(conversation_id=conv.id, user_id=uid))
    await session.commit()
    await session.refresh(conv, ["members"])
    return conv


async def get_member_conversation(
    session: AsyncSession, conversation_id: int, user_id: int
) -> Conversation | None:
    """Conversation where user_id is a member, else None."""
    row = (
        await session.execute(
            select(Conversation)
            .join(
                ConversationMember,
                ConversationMember.conversation_id == Conversation.id,
            )
            .where(
                Conversation.id == conversation_id,
                ConversationMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    return row


async def list_conversations(
    session: AsyncSession,
    user_id: int,
    status: str = "active",
    include_deleted: bool = False,
) -> list[Conversation]:
    q = (
        select(Conversation)
        .join(
            ConversationMember,
            ConversationMember.conversation_id == Conversation.id,
        )
        .where(ConversationMember.user_id == user_id)
        .order_by(Conversation.updated_at.desc())
    )
    if status != "all":
        q = q.where(Conversation.status == status)
    elif not include_deleted:
        q = q.where(Conversation.status != "deleted")
    return list((await session.execute(q)).scalars())


async def update_conversation(
    session: AsyncSession,
    conv: Conversation,
    *,
    title: str | None = None,
    reply_mode: str | None = None,
    status: str | None = None,
) -> Conversation:
    if title is not None:
        conv.title = title
    if reply_mode is not None:
        conv.reply_mode = reply_mode
    if status is not None and status != conv.status:
        conv.status = status
        # Keep archived_at on soft-delete so the archive timestamp survives.
        if status == "archived":
            conv.archived_at = utcnow()
        elif status == "active":
            conv.archived_at = None
    await session.commit()
    await session.refresh(conv)
    return conv


async def _is_telegram_group(session: AsyncSession, conversation_id: int) -> bool:
    bound = (
        await session.execute(
            select(TelegramBinding.conversation_id)
            .where(
                TelegramBinding.conversation_id == conversation_id,
                TelegramBinding.kind.in_(("group", "topic")),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return bound is not None


async def _sync_kind(
    session: AsyncSession, conv: Conversation, member_count: int
) -> None:
    """Re-derive kind after a membership change: more than one member makes the
    session a group, a single member makes it personal. A conversation bound to
    a telegram group/topic stays "group" — unlinked chat participants still
    write there."""
    kind = (
        "group"
        if member_count > 1 or await _is_telegram_group(session, conv.id)
        else "personal"
    )
    if kind == conv.kind:
        return
    conv.kind = kind
    conv.reply_mode = "mention" if kind == "group" else "always"


async def add_members(
    session: AsyncSession, conv: Conversation, user_ids: list[int]
) -> list[int] | None:
    existing_members = {m.user_id for m in conv.members}
    wanted = set(user_ids) - existing_members
    if not wanted:
        return []
    found = set(
        (await session.execute(select(User.id).where(User.id.in_(wanted)))).scalars()
    )
    if found != wanted:
        return None
    for uid in wanted:
        session.add(ConversationMember(conversation_id=conv.id, user_id=uid))
    await _sync_kind(session, conv, len(existing_members) + len(wanted))
    await session.commit()
    # _sync_kind UPDATEs the row and expires the onupdate-computed updated_at.
    await session.refresh(conv)
    await session.refresh(conv, ["members"])
    return sorted(wanted)


async def remove_member(
    session: AsyncSession, conv: Conversation, user_id: int
) -> bool:
    member = await session.get(ConversationMember, (conv.id, user_id))
    if member is None:
        return False
    await session.delete(member)
    await _sync_kind(session, conv, len(conv.members) - 1)
    await session.commit()
    await session.refresh(conv)
    await session.refresh(conv, ["members"])
    return True


async def post_message(
    session: AsyncSession,
    conv: Conversation,
    author: User,
    parts: list[dict[str, Any]],
    client_msg_id: str | None,
    source_channel: str = "web",
) -> tuple[Message, bool]:
    """Append a message; idempotent on (conversation, author, client_msg_id)."""
    if client_msg_id is not None:
        existing = (
            await session.execute(
                select(Message).where(
                    Message.conversation_id == conv.id,
                    Message.author_id == author.id,
                    Message.client_msg_id == client_msg_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

    # Serialize seq assignment on the conversation row (Postgres; no-op on sqlite,
    # where the uq_messages_conv_seq constraint is the backstop).
    await session.execute(
        select(Conversation.id)
        .where(Conversation.id == conv.id)
        .with_for_update()
    )
    next_seq = (
        await session.execute(
            select(func.coalesce(func.max(Message.seq), 0) + 1).where(
                Message.conversation_id == conv.id
            )
        )
    ).scalar_one()
    msg = Message(
        conversation_id=conv.id,
        seq=next_seq,
        author_kind="user",
        author_id=author.id,
        parts=parts,
        source_channel=source_channel,
        client_msg_id=client_msg_id,
    )
    session.add(msg)
    conv.updated_at = utcnow()
    member = await session.get(ConversationMember, (conv.id, author.id))
    if member is not None:
        member.last_read_seq = next_seq
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        if client_msg_id is None:
            raise
        existing = (
            await session.execute(
                select(Message).where(
                    Message.conversation_id == conv.id,
                    Message.author_id == author.id,
                    Message.client_msg_id == client_msg_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing, False
    await session.refresh(msg)
    return msg, True


async def post_assistant_message(
    session: AsyncSession,
    conv: Conversation,
    parts: list[dict[str, Any]],
    run_id: int | None,
    author_kind: str = "assistant",
) -> Message:
    """Append an assistant/system message produced by the server."""
    await session.execute(
        select(Conversation.id)
        .where(Conversation.id == conv.id)
        .with_for_update()
    )
    next_seq = (
        await session.execute(
            select(func.coalesce(func.max(Message.seq), 0) + 1).where(
                Message.conversation_id == conv.id
            )
        )
    ).scalar_one()
    msg = Message(
        conversation_id=conv.id,
        seq=next_seq,
        author_kind=author_kind,
        author_id=None,
        parts=parts,
        run_id=run_id,
    )
    session.add(msg)
    conv.updated_at = utcnow()
    await session.commit()
    await session.refresh(msg)
    return msg


async def post_system_message(
    session: AsyncSession, conv: Conversation, parts: list[dict[str, Any]]
) -> Message:
    """Append a system notice (e.g. STT failure) — not an agent reply."""
    return await post_assistant_message(session, conv, parts, None, "system")


async def delete_message(session: AsyncSession, msg: Message) -> None:
    """Delete a message row; clear FKs that point at it (runs, memory)."""
    await session.execute(
        update(Run)
        .where(Run.trigger_message_id == msg.id)
        .values(trigger_message_id=None)
    )
    await session.execute(
        update(MemoryItem)
        .where(MemoryItem.source_message_id == msg.id)
        .values(source_message_id=None)
    )
    await session.delete(msg)
    await session.commit()


async def list_messages(
    session: AsyncSession,
    conversation_id: int,
    before_seq: int | None,
    limit: int,
) -> list[Message]:
    q = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq.desc())
        .limit(limit)
    )
    if before_seq is not None:
        q = q.where(Message.seq < before_seq)
    rows = list((await session.execute(q)).scalars())
    rows.reverse()
    return rows


async def mark_read(
    session: AsyncSession, conversation_id: int, user_id: int, seq: int
) -> int | None:
    member = await session.get(ConversationMember, (conversation_id, user_id))
    if member is None:
        return None
    if seq > member.last_read_seq:
        member.last_read_seq = seq
        await session.commit()
    return member.last_read_seq


async def search_messages(
    session: AsyncSession, user_id: int, query: str, limit: int
) -> list[Message]:
    member_conv = (
        select(ConversationMember.conversation_id)
        .where(ConversationMember.user_id == user_id)
        .scalar_subquery()
    )
    # Postgres: GIN-indexed generated column messages.tsv; sqlite (tests): LIKE
    # over the serialized parts.
    if session.get_bind().dialect.name == "postgresql":
        cond = literal_column("messages.tsv").op("@@")(
            func.plainto_tsquery("simple", query)
        )
    else:
        cond = cast(Message.parts, String).ilike(f"%{query}%")
    q = (
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.conversation_id.in_(member_conv),
            Conversation.status != "deleted",
            cond,
        )
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(q)).scalars())
