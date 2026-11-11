"""Telegram gateway: the only module that imports aiogram.

Owns the Bot + Dispatcher polling task and the settings-driven supervisor
(`telegram.enabled` / `telegram.bot_token` toggles apply without restart).
"""

import asyncio
import contextlib
import logging
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from concierge.channels import get_hub
from concierge.channels.telegram.models import TelegramBinding
from concierge.channels.telegram.service import (
    InboundMedia,
    InboundMessage,
    TelegramOutbox,
    TelegramService,
)
from concierge.channels.telegram.sink import TelegramSink
from concierge.conversations.models import Conversation
from concierge.core.db import get_sessionmaker
from concierge.media import service as media_service
from concierge.settings import service as settings_service
from concierge.settings.models import SettingScope

log = logging.getLogger(__name__)

POLL_CHECK_INTERVAL = 10.0
TG_DOWNLOAD_MAX = 20 * 1024 * 1024

BOT_COMMANDS = [
    BotCommand(command="new", description="Новая сессия"),
    BotCommand(command="sessions", description="Выбрать сессию"),
    BotCommand(command="archive", description="Архивировать сессию"),
    BotCommand(command="voice", description="Озвучка ответов on|off"),
    BotCommand(command="help", description="Справка"),
]


class AiogramOutbox:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    def _thread(self, thread_id: int) -> int | None:
        return thread_id or None

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int,
        parse_mode: str | None = None,
        reply_markup: list[list[tuple[str, str]]] | None = None,
    ) -> int:
        markup = None
        if reply_markup:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text=label, callback_data=data)
                        for label, data in row
                    ]
                    for row in reply_markup
                ]
            )
        msg = await self._bot.send_message(
            chat_id,
            text,
            message_thread_id=self._thread(thread_id),
            parse_mode=parse_mode,
            reply_markup=markup,
        )
        return msg.message_id

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> None:
        await self._bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, parse_mode=parse_mode
        )

    async def send_document(
        self,
        chat_id: int,
        data: bytes,
        filename: str,
        *,
        thread_id: int,
        caption: str | None = None,
    ) -> int:
        msg = await self._bot.send_document(
            chat_id,
            BufferedInputFile(data, filename=filename),
            message_thread_id=self._thread(thread_id),
            caption=caption,
        )
        return msg.message_id

    async def send_voice(
        self, chat_id: int, data: bytes, *, thread_id: int
    ) -> int:
        msg = await self._bot.send_voice(
            chat_id,
            BufferedInputFile(data, filename="voice.ogg"),
            message_thread_id=self._thread(thread_id),
        )
        return msg.message_id

    async def send_audio(
        self, chat_id: int, data: bytes, filename: str, *, thread_id: int
    ) -> int:
        msg = await self._bot.send_audio(
            chat_id,
            BufferedInputFile(data, filename=filename),
            message_thread_id=self._thread(thread_id),
        )
        return msg.message_id

    async def chat_action(
        self, chat_id: int, action: str, *, thread_id: int
    ) -> None:
        await self._bot.send_chat_action(
            chat_id, action, message_thread_id=self._thread(thread_id)
        )

    async def answer_callback(
        self, callback_id: str, text: str | None = None
    ) -> None:
        await self._bot.answer_callback_query(callback_id, text=text)


class TelegramGateway:
    """Polling task + hub subscriptions for one bot instance."""

    def __init__(self, outbox_factory: Any = None) -> None:
        self._outbox_factory = outbox_factory or AiogramOutbox
        self._bot: Bot | None = None
        self._bot_id = 0
        self._bot_username = ""
        self._task: asyncio.Task[None] | None = None
        self._service: TelegramService | None = None
        self._outbox: TelegramOutbox | None = None
        self._sinks: dict[tuple[int, int], Any] = {}
        self.running = False

    async def start(self, token: str) -> None:
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
        self._bot = bot
        outbox: TelegramOutbox = self._outbox_factory(bot)
        self._outbox = outbox
        self._service = TelegramService(outbox, self._binding_changed)
        me = await bot.get_me()
        self._bot_id = me.id
        self._bot_username = me.username or ""
        dp = Dispatcher()
        dp.message.register(self._on_message)
        dp.callback_query.register(self._on_callback)
        async with get_sessionmaker()() as session:
            if self._bot_username:
                await settings_service.set_value(
                    session,
                    SettingScope.global_,
                    0,
                    "telegram.bot_username",
                    self._bot_username,
                    None,
                )
            rows = (
                await session.execute(select(TelegramBinding))
            ).scalars().all()
        for binding in rows:
            await self._binding_changed(
                binding.chat_id, binding.thread_id, binding.conversation_id
            )
        await bot.set_my_commands(BOT_COMMANDS)
        self._task = asyncio.create_task(dp.start_polling(bot))
        self.running = True

    async def stop(self) -> None:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        hub = get_hub()
        for _key, (_sink, conn, _conv) in self._sinks.items():
            await hub.disconnect(conn)
        self._sinks.clear()
        if self._bot is not None:
            await self._bot.session.close()
            self._bot = None

    async def _binding_changed(
        self, chat_id: int, thread_id: int, conv_id: int | None
    ) -> None:
        key = (chat_id, thread_id)
        hub = get_hub()
        existing = self._sinks.get(key)
        if conv_id is None:
            if existing is not None:
                _sink, conn, _old = existing
                await hub.disconnect(conn)
                del self._sinks[key]
            return
        if existing is not None:
            _sink, conn, old_conv = existing
            if old_conv == conv_id:
                return
            hub.unsubscribe(conn, old_conv)
            await hub.subscribe(conn, conv_id)
            self._sinks[key] = (_sink, conn, conv_id)
            return
        user_id = await self._dm_user(chat_id, thread_id)
        assert self._outbox is not None
        sink = TelegramSink(self._outbox, chat_id, thread_id, user_id)
        conn = hub.connect(sink)
        await hub.subscribe(conn, conv_id)
        self._sinks[key] = (sink, conn, conv_id)

    async def _dm_user(self, chat_id: int, thread_id: int) -> int:
        """Linked user for a dm_current binding; 0 for group sinks."""
        if thread_id != 0:
            return 0
        async with get_sessionmaker()() as session:
            binding = await session.get(TelegramBinding, (chat_id, thread_id))
            if binding is None or binding.kind != "dm_current":
                return 0
            conv = await session.get(
                Conversation, binding.conversation_id
            )
            return conv.owner_id if conv is not None else 0

    # ---------- aiogram handlers ----------

    async def _on_message(self, m: Message) -> None:
        assert self._service is not None
        im = await self._to_inbound(m)
        if im is not None:
            await self._service.handle_message(im)

    async def _on_callback(self, cb: CallbackQuery) -> None:
        assert self._service is not None
        if cb.message is None or cb.from_user is None:
            await self._service.handle_callback(cb.id, 0, 0, cb.data or "")
            return
        await self._service.handle_callback(
            cb.id, cb.message.chat.id, cb.from_user.id, cb.data or ""
        )

    async def _to_inbound(self, m: Message) -> InboundMessage | None:
        if m.from_user is None:
            return None
        text = m.text or m.caption or ""
        command = command_arg = None
        if text.startswith("/"):
            head, _, arg = text.partition(" ")
            raw = head[1:].split("@", 1)[0].lower()
            command, command_arg = raw, arg.strip()
        mentioned = False
        for ent in m.entities or m.caption_entities or []:
            if ent.type == "mention" and self._bot_username:
                value = text[ent.offset : ent.offset + ent.length]
                if value.lower() == f"@{self._bot_username}".lower():
                    mentioned = True
        reply = m.reply_to_message
        reply_from_bot = bool(
            reply
            and reply.from_user
            and reply.from_user.id == self._bot_id
        )
        topic_title = None
        if reply is not None and reply.forum_topic_created is not None:
            topic_title = reply.forum_topic_created.name
        elif m.forum_topic_created is not None:
            topic_title = m.forum_topic_created.name
        media = await self._download_media(m)
        if media == "too_big":
            await m.answer("Файл слишком большой (лимит 20 МБ).")
            media = None
        return InboundMessage(
            chat_id=m.chat.id,
            message_id=m.message_id,
            tg_user_id=m.from_user.id,
            thread_id=m.message_thread_id or 0,
            is_private=m.chat.type == "private",
            is_forum_topic=bool(m.is_topic_message and m.message_thread_id),
            tg_username=m.from_user.username,
            text=text,
            media=media if isinstance(media, InboundMedia) else None,
            reply_to_from_bot=reply_from_bot,
            mentioned_bot=mentioned,
            command=command,
            command_arg=command_arg or "",
            topic_title=topic_title,
        )

    async def _download_media(self, m: Message) -> Any:
        assert self._bot is not None
        file_id = name = mime = None
        size = 0
        if m.voice is not None:
            file_id, size = m.voice.file_id, m.voice.file_size or 0
            name, mime = "voice.ogg", m.voice.mime_type or "audio/ogg"
        elif m.audio is not None:
            file_id, size = m.audio.file_id, m.audio.file_size or 0
            name = m.audio.file_name or "audio"
            mime = m.audio.mime_type or "application/octet-stream"
        elif (v := m.video or m.video_note) is not None:
            file_id, size = v.file_id, v.file_size or 0
            name = getattr(v, "file_name", None) or "video.mp4"
            mime = getattr(v, "mime_type", None) or "video/mp4"
        elif m.photo:
            p = m.photo[-1]
            file_id, size = p.file_id, p.file_size or 0
            name, mime = "photo.jpg", "image/jpeg"
        elif m.document is not None:
            file_id, size = m.document.file_id, m.document.file_size or 0
            name = m.document.file_name or "file"
            mime = m.document.mime_type or "application/octet-stream"
        if file_id is None:
            return None
        if size > TG_DOWNLOAD_MAX:
            return "too_big"
        buf = await self._bot.download(file_id)
        if buf is None:
            return None
        data = buf.read()
        assert name is not None and mime is not None
        return InboundMedia(
            kind=media_service.kind_of(mime, name),
            name=name,
            mime=mime,
            data=data,
        )


# ---------- supervisor ----------

_gateway: TelegramGateway | None = None
_gateway_token: str | None = None
_task: asyncio.Task[None] | None = None


def _make_gateway() -> TelegramGateway:
    return TelegramGateway()


async def _tick() -> None:
    """One supervisor iteration: reconcile desired state with the poller."""
    global _gateway, _gateway_token
    async with get_sessionmaker()() as session:
        enabled = bool(
            await settings_service.get_global(
                session, "telegram.enabled", False
            )
        )
        raw_token = await settings_service.get_global(
            session, "telegram.bot_token"
        )
    token = str(raw_token) if raw_token else None
    if enabled and token:
        if _gateway is not None and _gateway_token == token:
            return
        if _gateway is not None:
            await _gateway.stop()
        _gateway = _make_gateway()
        await _gateway.start(token)
        _gateway_token = token
    elif _gateway is not None:
        await _gateway.stop()
        _gateway = None
        _gateway_token = None


async def _loop() -> None:
    while True:
        try:
            await _tick()
        except Exception:
            log.exception("telegram supervisor tick failed")
        await asyncio.sleep(POLL_CHECK_INTERVAL)


def start_supervisor() -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_loop())


async def stop_supervisor() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
        _task = None
    if _gateway is not None:
        await _gateway.stop()


def is_running() -> bool:
    return _gateway is not None and _gateway.running


async def binding_dropped(chat_id: int, thread_id: int) -> None:
    """API-side binding removal (unlink/archive) reaches the live gateway."""
    if _gateway is not None:
        await _gateway._binding_changed(chat_id, thread_id, None)
