"""Telegram channel: routing and business logic (aiogram-free).

`gateway.py` adapts aiogram objects to `InboundMessage` and implements the
real `TelegramOutbox`; tests inject a fake outbox and drive this service
directly.
"""

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.agent import runtime as agent_runtime
from concierge.auth.models import User
from concierge.auth.permissions import has_permission
from concierge.channels import ChannelEvent, get_hub
from concierge.channels.telegram.models import (
    TelegramBinding,
    TelegramLink,
    TelegramLinkCode,
    TelegramMessage,
)
from concierge.conversations import service as conversations
from concierge.conversations.models import Conversation, ConversationMember
from concierge.core import audit, ratelimit
from concierge.core.db import get_sessionmaker, utcnow
from concierge.media import service as media_service
from concierge.media.pipeline import get_pipeline
from concierge.memory import indexer as memory_indexer
from concierge.settings import service as settings_service
from concierge.settings.models import SettingScope

log = logging.getLogger(__name__)

PAIR_NOTICE_INTERVAL = 300.0
LINK_CODE_TTL_SECONDS = 600
SESSION_PICK_LIMIT = 8

PAIR_INSTRUCTION = (
    "Чтобы привязать Telegram, получите код в приложении "
    "(Настройки → Telegram) и отправьте «/start <код>»."
)
HELP_TEXT = (
    "/new — новая сессия\n"
    "/sessions — выбрать сессию\n"
    "/archive — архивировать текущую сессию\n"
    "/voice on|off — озвучивать ответы\n"
    "/help — эта справка"
)
_DM_COMMANDS = {"start", "new", "sessions", "archive", "voice", "help"}

BindingChanged = Callable[[int, int, int | None], Awaitable[None]]


@dataclass(slots=True)
class InboundMedia:
    kind: str  # image | audio | video | document | other
    name: str
    mime: str
    data: bytes


@dataclass(slots=True)
class InboundMessage:
    chat_id: int
    message_id: int
    tg_user_id: int
    thread_id: int = 0
    is_private: bool = False
    is_forum_topic: bool = False
    tg_username: str | None = None
    text: str = ""
    media: InboundMedia | None = None
    reply_to_from_bot: bool = False
    mentioned_bot: bool = False
    command: str | None = None
    command_arg: str = ""
    topic_title: str | None = None


class TelegramOutbox(Protocol):
    """Outbound calls; reply_markup is plain rows of (label, callback_data)."""

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int,
        parse_mode: str | None = None,
        reply_markup: list[list[tuple[str, str]]] | None = None,
    ) -> int: ...

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> None: ...

    async def send_document(
        self,
        chat_id: int,
        data: bytes,
        filename: str,
        *,
        thread_id: int,
        caption: str | None = None,
    ) -> int: ...

    async def send_voice(
        self, chat_id: int, data: bytes, *, thread_id: int
    ) -> int: ...

    async def send_audio(
        self, chat_id: int, data: bytes, filename: str, *, thread_id: int
    ) -> int: ...

    async def chat_action(
        self, chat_id: int, action: str, *, thread_id: int
    ) -> None: ...

    async def answer_callback(
        self, callback_id: str, text: str | None = None
    ) -> None: ...


def hash_link_code(code: str) -> str:
    return hashlib.sha256(code.strip().encode()).hexdigest()


async def notify_user_dm(user_id: int, text: str) -> None:
    """DM a linked user through the live gateway; no-op when unlinked/off."""
    async with get_sessionmaker()() as session:
        link = (
            await session.execute(
                select(TelegramLink).where(TelegramLink.user_id == user_id)
            )
        ).scalar_one_or_none()
    if link is None:
        return
    from concierge.channels.telegram import gateway

    await gateway.notify_user_dm(link.telegram_user_id, text)


class TelegramService:
    def __init__(
        self, outbox: TelegramOutbox, on_binding_changed: BindingChanged
    ) -> None:
        self._outbox = outbox
        self._on_binding_changed = on_binding_changed
        self._pair_notice: dict[int, float] = {}

    async def handle_message(self, im: InboundMessage) -> None:
        if not im.tg_user_id:
            return
        async with get_sessionmaker()() as session:
            link = await self._link_for(session, im.tg_user_id)
            if link is None:
                if not im.is_private:
                    return
                if (
                    im.command == "start"
                    and im.command_arg
                    and await self._try_link(session, im)
                ):
                    return
                await self._send_pair_instruction(im)
                return
            user = await session.get(User, link.user_id)
            if user is None or not await self._allowed(
                session, user, im.tg_user_id
            ):
                return
            if im.command is not None:
                await self._command(session, im, user)
                return
            if not im.text.strip() and im.media is None:
                return
            conv = await self._resolve_conversation(session, im, user)
            if conv is None:
                return
            built = await self._build_parts(session, im, conv, user)
            if built is None:
                return
            parts, notice_text = built
            msg, created = await conversations.post_message(
                session,
                conv,
                user,
                parts,
                f"tg:{im.chat_id}:{im.message_id}",
                source_channel="telegram",
            )
            await session.merge(
                TelegramMessage(
                    chat_id=im.chat_id,
                    message_id=im.message_id,
                    conversation_message_id=msg.id,
                )
            )
            notice = None
            if created and notice_text is not None:
                notice = await conversations.post_system_message(
                    session,
                    conv,
                    [{"type": "text", "text": notice_text}],
                )
            await session.commit()
            conv_id, msg_id = conv.id, msg.id
            out = agent_runtime._message_dict(msg)
            notice_out = (
                agent_runtime._message_dict(notice)
                if notice is not None
                else None
            )
        if created:
            await get_hub().publish(
                ChannelEvent(
                    "message.created", conv_id, {"message": out}
                )
            )
            if notice_out is not None:
                await get_hub().publish(
                    ChannelEvent(
                        "message.created",
                        conv_id,
                        {"message": notice_out},
                    )
                )
            memory_indexer.kick()
            agent_runtime.get_runtime().schedule(conv_id, msg_id)

    async def handle_callback(
        self,
        callback_id: str,
        chat_id: int,
        tg_user_id: int,
        data: str,
    ) -> None:
        async with get_sessionmaker()() as session:
            link = await self._link_for(session, tg_user_id)
            user = await session.get(User, link.user_id) if link else None
            if user is None:
                await self._outbox.answer_callback(callback_id)
                return
            if data.startswith("sel:"):
                try:
                    conv_id = int(data[4:])
                except ValueError:
                    await self._outbox.answer_callback(callback_id)
                    return
                conv = await conversations.get_member_conversation(
                    session, conv_id, user.id
                )
                if conv is None:
                    await self._outbox.answer_callback(
                        callback_id, "Сессия недоступна"
                    )
                    return
                await self._bind(
                    session, chat_id, 0, conv.id, "dm_current"
                )
                await self._outbox.answer_callback(callback_id, "Готово")
                await self._outbox.send(
                    chat_id,
                    f"Активная сессия: {conv.title or f'#{conv.id}'}",
                    thread_id=0,
                )
                return
            await self._outbox.answer_callback(callback_id)

    # ---------- auth / linking ----------

    async def _link_for(
        self, session: AsyncSession, tg_user_id: int
    ) -> TelegramLink | None:
        return (
            await session.execute(
                select(TelegramLink).where(
                    TelegramLink.telegram_user_id == tg_user_id
                )
            )
        ).scalar_one_or_none()

    async def _allowed(
        self, session: AsyncSession, user: User, tg_user_id: int
    ) -> bool:
        if not user.is_active or not has_permission(
            user.role.permissions, "telegram"
        ):
            return False
        allowed = await settings_service.get_global(
            session, "telegram.allowed_user_ids"
        )
        if isinstance(allowed, list) and allowed:
            return tg_user_id in {int(x) for x in allowed}
        return True

    async def _send_pair_instruction(self, im: InboundMessage) -> None:
        now = time.monotonic()
        last = self._pair_notice.get(im.tg_user_id, 0.0)
        if now - last < PAIR_NOTICE_INTERVAL:
            return
        self._pair_notice[im.tg_user_id] = now
        await self._outbox.send(
            im.chat_id, PAIR_INSTRUCTION, thread_id=im.thread_id
        )

    async def _try_link(
        self, session: AsyncSession, im: InboundMessage
    ) -> bool:
        limiter = ratelimit.get_limiter()
        pair_key = f"tg_pair:{im.tg_user_id}"
        if not limiter.check(
            pair_key, ratelimit.TG_PAIR_LIMIT, ratelimit.TG_PAIR_WINDOW
        ):
            log.warning(
                "telegram pairing rate-limited: user %s", im.tg_user_id
            )
            return False
        row = await session.get(
            TelegramLinkCode, hash_link_code(im.command_arg)
        )
        if row is None or row.expires_at < utcnow():
            limiter.allow(
                pair_key, ratelimit.TG_PAIR_LIMIT, ratelimit.TG_PAIR_WINDOW
            )
            return False
        user = await session.get(User, row.user_id)
        if user is None or not await self._allowed(
            session, user, im.tg_user_id
        ):
            return False
        existing = await self._link_for(session, im.tg_user_id)
        if existing is not None:
            await session.delete(existing)
        session.add(
            TelegramLink(
                user_id=user.id,
                telegram_user_id=im.tg_user_id,
                telegram_username=im.tg_username,
            )
        )
        await session.delete(row)
        audit.record(
            session,
            "telegram.link",
            actor_id=user.id,
            actor_name=user.username,
            target=str(im.tg_user_id),
            detail={"telegram_username": im.tg_username},
        )
        limiter.reset(pair_key)
        await session.commit()
        await self._outbox.send(
            im.chat_id,
            f"Telegram привязан к {user.display_name}. {HELP_TEXT}",
            thread_id=0,
        )
        return True

    # ---------- commands ----------

    async def _command(
        self, session: AsyncSession, im: InboundMessage, user: User
    ) -> None:
        if not im.is_private:
            if im.command == "archive":
                await self._outbox.send(
                    im.chat_id,
                    "Команда /archive недоступна в группе.",
                    thread_id=im.thread_id,
                )
            return
        cmd = im.command
        if cmd == "start":
            await self._outbox.send(
                im.chat_id, f"Привет! {HELP_TEXT}", thread_id=0
            )
        elif cmd == "new":
            conv = await conversations.create_conversation(
                session, user, "personal", "", [], origin="telegram"
            )
            assert conv is not None
            await self._bind(session, im.chat_id, 0, conv.id, "dm_current")
            await self._outbox.send(
                im.chat_id, "Новая сессия создана.", thread_id=0
            )
        elif cmd == "sessions":
            convs = [
                c
                for c in await conversations.list_conversations(
                    session, user.id
                )
                if c.kind == "personal"
            ][:SESSION_PICK_LIMIT]
            if not convs:
                await self._outbox.send(
                    im.chat_id, "Нет активных сессий.", thread_id=0
                )
                return
            markup = [
                [(c.title or f"#{c.id}", f"sel:{c.id}")] for c in convs
            ]
            await self._outbox.send(
                im.chat_id,
                "Выберите сессию:",
                thread_id=0,
                reply_markup=markup,
            )
        elif cmd == "archive":
            await self._archive(session, im)
        elif cmd == "voice":
            await self._voice(session, im, user)
        else:
            await self._outbox.send(im.chat_id, HELP_TEXT, thread_id=0)

    async def _archive(self, session: AsyncSession, im: InboundMessage) -> None:
        binding = await session.get(TelegramBinding, (im.chat_id, 0))
        conv = (
            await session.get(Conversation, binding.conversation_id)
            if binding is not None
            else None
        )
        if conv is None:
            await self._outbox.send(
                im.chat_id, "Нет активной сессии.", thread_id=0
            )
            return
        await conversations.update_conversation(
            session, conv, status="archived"
        )
        memory_indexer.schedule_archive(conv.id)
        assert binding is not None
        await session.delete(binding)
        await session.commit()
        await self._on_binding_changed(im.chat_id, 0, None)
        await self._outbox.send(
            im.chat_id, "Сессия архивирована.", thread_id=0
        )

    async def _voice(
        self, session: AsyncSession, im: InboundMessage, user: User
    ) -> None:
        arg = im.command_arg.strip().lower()
        if arg in ("on", "off"):
            mode = "always" if arg == "on" else "off"
            await settings_service.set_value(
                session,
                SettingScope.user,
                user.id,
                "telegram.voice_reply",
                mode,
                user.id,
            )
            await self._outbox.send(
                im.chat_id,
                f"Озвучка ответов: {arg}.",
                thread_id=0,
            )
            return
        mode = await settings_service.get_value(
            session, user, "telegram.voice_reply", "off"
        )
        await self._outbox.send(
            im.chat_id, f"Озвучка ответов: {mode}.", thread_id=0
        )

    # ---------- routing ----------

    async def _bind(
        self,
        session: AsyncSession,
        chat_id: int,
        thread_id: int,
        conv_id: int,
        kind: str,
    ) -> None:
        binding = await session.get(TelegramBinding, (chat_id, thread_id))
        if binding is None:
            session.add(
                TelegramBinding(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    conversation_id=conv_id,
                    kind=kind,
                )
            )
        else:
            binding.conversation_id = conv_id
            binding.kind = kind
        await session.commit()
        await self._on_binding_changed(chat_id, thread_id, conv_id)

    async def _resolve_conversation(
        self, session: AsyncSession, im: InboundMessage, user: User
    ) -> Conversation | None:
        if im.is_private:
            return await self._resolve_dm(session, im, user)
        return await self._resolve_group(session, im, user)

    async def _resolve_dm(
        self, session: AsyncSession, im: InboundMessage, user: User
    ) -> Conversation | None:
        binding = await session.get(TelegramBinding, (im.chat_id, 0))
        conv = (
            await session.get(Conversation, binding.conversation_id)
            if binding is not None
            else None
        )
        if (
            conv is not None
            and conv.status == "active"
            and conv.kind == "personal"
        ):
            return conv
        for candidate in await conversations.list_conversations(
            session, user.id
        ):
            if candidate.kind == "personal":
                await self._bind(
                    session, im.chat_id, 0, candidate.id, "dm_current"
                )
                return candidate
        conv = await conversations.create_conversation(
            session, user, "personal", "", [], origin="telegram"
        )
        assert conv is not None
        await self._bind(session, im.chat_id, 0, conv.id, "dm_current")
        return conv

    async def _resolve_group(
        self, session: AsyncSession, im: InboundMessage, user: User
    ) -> Conversation | None:
        thread_id = im.thread_id if im.is_forum_topic else 0
        binding = await session.get(
            TelegramBinding, (im.chat_id, thread_id)
        )
        conv = (
            await session.get(Conversation, binding.conversation_id)
            if binding is not None
            else None
        )
        if conv is not None and conv.status == "active":
            member = await session.get(
                ConversationMember, (conv.id, user.id)
            )
            if member is None:
                await conversations.add_members(session, conv, [user.id])
            return conv
        if im.is_forum_topic:
            title = im.topic_title or f"Тема #{thread_id}"
            kind = "topic"
        else:
            title = f"Telegram-чат {im.chat_id}"
            kind = "group"
        conv = await conversations.create_conversation(
            session, user, "group", title, [], origin="telegram"
        )
        assert conv is not None
        await self._bind(session, im.chat_id, thread_id, conv.id, kind)
        return conv

    # ---------- parts ----------

    async def _build_parts(
        self,
        session: AsyncSession,
        im: InboundMessage,
        conv: Conversation,
        user: User,
    ) -> tuple[list[dict[str, Any]], str | None] | None:
        """Message parts + an optional system-notice text to post after them."""
        parts: list[dict[str, Any]] = []
        notice: str | None = None
        media = im.media
        if media is not None:
            max_bytes = media_service.max_upload_bytes(user)
            if max_bytes <= 0 or len(media.data) > max_bytes:
                await self._outbox.send(
                    im.chat_id,
                    "Файл слишком большой или загрузки запрещены.",
                    thread_id=im.thread_id,
                )
                return None
            att, _ = await media_service.create_attachment(
                session, user, media.name, media.mime, media.data, conv.id
            )
            get_pipeline().schedule(att.id)
            media_part: dict[str, Any] = {
                "type": media.kind,
                "attachment_id": att.id,
                "name": media.name,
                "mime": media.mime,
            }
            if im.is_private and not im.text.strip() and media.kind == "audio":
                transcript = (await media_service.transcribe(att.id)).strip()
                await session.refresh(att)
                if transcript:
                    parts.append(
                        {
                            "type": "text",
                            "text": transcript,
                            "transcribed": True,
                        }
                    )
                    parts.append(media_part)
                else:
                    parts.append(media_part)
                    notice = "Ошибка распознавания голосового сообщения."
            else:
                parts.extend(
                    [{"type": "text", "text": im.text}] if im.text.strip() else []
                )
                parts.append(media_part)
        elif im.text.strip():
            parts.append({"type": "text", "text": im.text})
        if (
            conv.kind == "group"
            and (im.mentioned_bot or im.reply_to_from_bot)
        ):
            parts.append({"type": "mention"})
        return (parts, notice) if parts else None


async def unlink_user(
    session: AsyncSession, user_id: int
) -> list[tuple[int, int]]:
    """Delete a user's telegram link + dm_current bindings (shared by the
    user/admin endpoints). Returns dropped (chat_id, thread_id) pairs."""
    personal = (
        select(ConversationMember.conversation_id).where(
            ConversationMember.user_id == user_id
        )
    ).scalar_subquery()
    rows = (
        await session.execute(
            select(TelegramBinding).where(
                TelegramBinding.kind == "dm_current",
                TelegramBinding.conversation_id.in_(personal),
            )
        )
    ).scalars().all()
    for binding in rows:
        await session.delete(binding)
    link = await session.get(TelegramLink, user_id)
    if link is not None:
        await session.delete(link)
    await session.commit()
    return [(b.chat_id, b.thread_id) for b in rows]
