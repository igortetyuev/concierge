"""TelegramSink: mirrors hub events into a bound chat/topic.

Streaming renders as edits of a single message (plain text, ~1 s throttle);
the assistant's final message.created re-renders it as MarkdownV2.
"""

import asyncio
import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.channels.hub import ChannelEvent
from concierge.channels.telegram.format import (
    MAX_LEN,
    escape_mdv2,
    md_to_mdv2,
    split_telegram,
)
from concierge.channels.telegram.models import TelegramMessage
from concierge.channels.telegram.service import (
    TelegramOutbox,
    _clip,
    checklist_markup,
    checklist_mask,
)
from concierge.conversations.models import Conversation, Message, Run
from concierge.core.config import get_settings
from concierge.core.db import get_sessionmaker
from concierge.media import service as media_service
from concierge.media import tts
from concierge.media.models import Attachment
from concierge.settings import service as settings_service

log = logging.getLogger(__name__)

EDIT_THROTTLE = 1.0


@dataclass
class _Stream:
    tg_msg_id: int | None = None
    buffer: str = ""
    tools: dict[str, dict[str, str]] = field(default_factory=dict)
    last_edit: float = 0.0
    last_text: str = ""


def _text_of(parts: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(p.get("text", ""))
        for p in parts
        if p.get("type") == "text" and str(p.get("text", "")).strip()
    )


class TelegramSink:
    """One bound (chat_id, thread_id); user_id is the linked user or 0."""

    presence = False

    def __init__(
        self,
        outbox: TelegramOutbox,
        chat_id: int,
        thread_id: int,
        user_id: int = 0,
    ) -> None:
        self._outbox = outbox
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.user_id = user_id
        self._runs: dict[int, _Stream] = {}

    async def emit(self, event: ChannelEvent) -> None:
        # Never raise into the Hub: a dropped connection here would go
        # unnoticed by the gateway's sink registry and silence the chat.
        try:
            await self._emit(event)
        except Exception:
            log.exception(
                "telegram sink emit failed chat=%s", self.chat_id
            )

    async def _emit(self, event: ChannelEvent) -> None:
        if event.type == "run.status":
            if event.data.get("status") == "running":
                await self._outbox.chat_action(
                    self.chat_id, "typing", thread_id=self.thread_id
                )
        elif event.type == "message.delta":
            run_id = event.data.get("run_id")
            if isinstance(run_id, int):
                st = self._runs.setdefault(run_id, _Stream())
                st.buffer += str(event.data.get("text", ""))
                await self._flush(st)
        elif event.type == "message.tool":
            run_id = event.data.get("run_id")
            if isinstance(run_id, int) and event.data.get("status") == "running":
                st = self._runs.setdefault(run_id, _Stream())
                call_id = str(
                    event.data.get("tool_call_id")
                    or event.data.get("name", "tool")
                )
                entry = st.tools.setdefault(
                    call_id, {"name": str(event.data.get("name", "tool"))}
                )
                if event.data.get("progress"):
                    entry["progress"] = str(event.data["progress"])
                await self._flush(st)
        elif event.type == "message.created":
            await self._message_created(event.data.get("message") or {})

    # ---------- streaming ----------

    def _render(self, st: _Stream) -> str:
        # Cap the streaming preview so plain edits can't hit the 4096 limit.
        text = st.buffer
        if len(text) > MAX_LEN - 100:
            text = "…" + text[-(MAX_LEN - 101) :]
        if st.tools:
            last = next(reversed(st.tools.values()))
            progress = last.get("progress")
            suffix = (
                f"🔧 {last['name']}: {progress}…"
                if progress
                else f"🔧 {last['name']}…"
            )
            text = f"{text}\n{suffix}" if text else suffix
        return text

    async def _flush(self, st: _Stream) -> None:
        text = self._render(st)
        if not text:
            return
        now = time.monotonic()
        if st.tg_msg_id is None:
            st.tg_msg_id = await self._outbox.send(
                self.chat_id, text, thread_id=self.thread_id
            )
            st.last_edit = now
            st.last_text = text
        elif now - st.last_edit >= EDIT_THROTTLE and text != st.last_text:
            await self._edit(st, text)
            st.last_edit = now
            st.last_text = text

    async def _edit(self, st: _Stream, text: str) -> None:
        assert st.tg_msg_id is not None
        await self._outbox.edit(self.chat_id, st.tg_msg_id, text)

    # ---------- message.created ----------

    async def _message_created(self, message: dict[str, Any]) -> None:
        # anti-echo: our own inbound is already in the chat; scheduler
        # triggers are shown to nobody — only the agent's reply goes out.
        if message.get("source_channel") in ("telegram", "schedule"):
            return
        kind = message.get("author_kind")
        if kind == "assistant":
            await self._assistant_message(message)
        elif kind in ("user", "system"):
            await self._mirror(message)

    async def _assistant_message(self, message: dict[str, Any]) -> None:
        text = _text_of(message.get("parts") or [])
        run_id = message.get("run_id")
        st = (
            self._runs.pop(run_id, None)
            if isinstance(run_id, int)
            else None
        )
        if text:
            raw = split_telegram(text)
            mdv2 = [md_to_mdv2(c) for c in raw]
            first = True
            if st is not None and st.tg_msg_id is not None:
                try:
                    await self._outbox.edit(
                        self.chat_id,
                        st.tg_msg_id,
                        mdv2[0],
                        parse_mode="MarkdownV2",
                    )
                except Exception:
                    await self._outbox.edit(
                        self.chat_id, st.tg_msg_id, raw[0]
                    )
                await self._record(
                    st.tg_msg_id, message.get("id")
                )
                first = False
            for i in range(int(not first), len(raw)):
                tg_id = await self._send_mdv2(mdv2[i], raw[i])
                await self._record(tg_id, message.get("id"))
        elif st is not None and st.tg_msg_id is not None:
            await self._record(st.tg_msg_id, message.get("id"))
        await self._send_part_attachments(message)
        await self._send_choice(message)
        await self._send_checklist(message)
        if isinstance(run_id, int):
            await self._maybe_voice_reply(message)

    async def _send_choice(self, message: dict[str, Any]) -> None:
        """An unanswered `choice` part: single-question single-select with
        options gets an inline keyboard (`ch:{message_id}:{idx}`); anything
        else degrades to numbered text the user answers freely."""
        part = next(
            (
                p
                for p in message.get("parts") or []
                if p.get("type") == "choice" and not p.get("answered")
            ),
            None,
        )
        if part is None:
            return
        questions = part.get("questions") or []
        if (
            len(questions) == 1
            and not questions[0].get("multi_select")
            and questions[0].get("options")
        ):
            q = questions[0]
            markup = [
                [
                    (
                        str(o.get("label") or f"Вариант {i + 1}"),
                        f"ch:{message.get('id')}:{i}",
                    )
                ]
                for i, o in enumerate(q["options"])
            ]
            text = str(q.get("question") or "Выберите:")
            tg_id = await self._outbox.send(
                self.chat_id,
                text,
                thread_id=self.thread_id,
                reply_markup=markup,
            )
            await self._record(tg_id, message.get("id"))
            return
        lines: list[str] = []
        for i, q in enumerate(questions, 1):
            lines.append(f"{i}. {q.get('question', '')}")
            for j, o in enumerate(q.get("options") or [], 1):
                desc = str(o.get("description") or "")
                opt = f"   {j}) {o.get('label', '')}"
                lines.append(f"{opt} — {desc}" if desc else opt)
        if lines:
            lines.append("Ответьте номером или текстом.")
            tg_id = await self._outbox.send(
                self.chat_id,
                "\n".join(lines),
                thread_id=self.thread_id,
            )
            await self._record(tg_id, message.get("id"))

    async def _send_checklist(self, message: dict[str, Any]) -> None:
        """A `checklist` part: numbered listing plus a toggle keyboard
        (see `service.checklist_markup` for the callback-data format)."""
        part = next(
            (
                p
                for p in message.get("parts") or []
                if p.get("type") == "checklist"
            ),
            None,
        )
        items = (part or {}).get("items") or []
        if part is None or not items:
            return
        mid = message.get("id")
        markup = checklist_markup(mid, part, checklist_mask(part))
        title = _clip(str(part.get("title") or "Чек-лист"), 200)
        texts = [_clip(str(it.get("text") or ""), 200) for it in items]
        # The listing can outgrow one message (50 long items > 4096):
        # split it into (mdv2, raw) chunk pairs; the keyboard rides the last.
        pairs: list[tuple[str, str]] = []
        cur_m = f"*{escape_mdv2(title)}*"
        cur_r = title
        for i, t in enumerate(texts):
            m_line = f"{i + 1}\\. {escape_mdv2(t)}"
            r_line = f"{i + 1}. {t}"
            if len(cur_m) + 1 + len(m_line) > MAX_LEN:
                pairs.append((cur_m, cur_r))
                cur_m, cur_r = m_line, r_line
            else:
                cur_m += "\n" + m_line
                cur_r += "\n" + r_line
        pairs.append((cur_m, cur_r))
        for i, (chunk_mdv2, chunk_raw) in enumerate(pairs):
            tg_id = await self._send_mdv2(
                chunk_mdv2,
                chunk_raw,
                reply_markup=markup if i == len(pairs) - 1 else None,
            )
            await self._record(tg_id, mid)

    async def _send_mdv2(
        self,
        chunk_mdv2: str,
        chunk_raw: str,
        reply_markup: list[list[tuple[str, str]]] | None = None,
    ) -> int:
        try:
            return await self._outbox.send(
                self.chat_id,
                chunk_mdv2,
                thread_id=self.thread_id,
                parse_mode="MarkdownV2",
                reply_markup=reply_markup,
            )
        except Exception:
            return await self._outbox.send(
                self.chat_id,
                chunk_raw,
                thread_id=self.thread_id,
                reply_markup=reply_markup,
            )

    async def _mirror(self, message: dict[str, Any]) -> None:
        text = _text_of(message.get("parts") or [])
        prefix = ""
        if message.get("author_kind") == "user" and message.get("author_id"):
            name = await self._user_name(int(message["author_id"]))
            prefix = f"{name}: " if name else ""
        if text:
            for chunk in split_telegram(f"{prefix}{text}"):
                tg_id = await self._outbox.send(
                    self.chat_id, chunk, thread_id=self.thread_id
                )
                await self._record(tg_id, message.get("id"))
        await self._send_part_attachments(message)

    async def _send_part_attachments(self, message: dict[str, Any]) -> None:
        for part in message.get("parts") or []:
            aid = media_service.part_attachment_id(part)
            if aid is None:
                continue
            blob = await self._attachment_blob(aid)
            if blob is None:
                continue
            data, att = blob
            name = att.original_name or str(part.get("name") or "file")
            if part.get("type") == "audio" and part.get("tts"):
                tg_id = await self._outbox.send_voice(
                    self.chat_id, data, thread_id=self.thread_id
                )
            else:
                tg_id = await self._outbox.send_document(
                    self.chat_id,
                    data,
                    name,
                    thread_id=self.thread_id,
                )
            await self._record(tg_id, message.get("id"))

    async def _attachment_blob(
        self, attachment_id: int
    ) -> tuple[bytes, Attachment] | None:
        async with get_sessionmaker()() as session:
            att = await session.get(Attachment, attachment_id)
            if att is None:
                return None
            try:
                return media_service.read_blob(att.sha256), att
            except OSError:
                return None

    async def _user_name(self, user_id: int) -> str:
        async with get_sessionmaker()() as session:
            user = await session.get(User, user_id)
            return user.display_name if user else ""

    async def _record(
        self, tg_message_id: int, conversation_message_id: Any
    ) -> None:
        mid = conversation_message_id if isinstance(
            conversation_message_id, int
        ) else None
        try:
            async with get_sessionmaker()() as session:
                await session.merge(
                    TelegramMessage(
                        chat_id=self.chat_id,
                        message_id=tg_message_id,
                        conversation_message_id=mid,
                    )
                )
                await session.commit()
        except Exception:
            log.exception("telegram_messages record failed")

    # ---------- voice replies ----------

    async def _maybe_voice_reply(self, message: dict[str, Any]) -> None:
        run_id = message["run_id"]
        text = _text_of(message.get("parts") or [])
        if not text.strip():
            return
        session: AsyncSession
        conv_id = message.get("conversation_id")
        if not isinstance(conv_id, int):
            return
        async with get_sessionmaker()() as session:
            conv = await session.get(Conversation, conv_id)
            run = await session.get(Run, run_id)
            if conv is None or run is None or conv.origin == "voice_quick":
                return
            trigger = (
                await session.get(Message, run.trigger_message_id)
                if run.trigger_message_id is not None
                else None
            )
            user = (
                await session.get(User, trigger.author_id)
                if trigger is not None and trigger.author_id is not None
                else None
            )
            if user is None:
                return
            mode = await settings_service.get_value(
                session, user, "telegram.voice_reply", "off"
            )
            requested = mode == "always" or (
                mode == "on-request"
                and trigger is not None
                and any(
                    p.get("type") == "audio" for p in trigger.parts
                )
            )
            if not requested:
                return
            override = tts.spoken_override(message.get("parts") or [])
            spoken = (
                tts.speechify(override)[: tts.TTS_TEXT_MAX]
                if override is not None
                else await tts.spoken_version(session, user, text)
            )
            audio = await tts.synthesize_speech(session, user, spoken)
        if audio is None:
            return
        data, mime = audio
        try:
            ogg = await _to_ogg(data, mime)
        except Exception:
            name = "reply.mp3" if mime == "audio/mpeg" else "reply.wav"
            await self._outbox.send_audio(
                self.chat_id, data, name, thread_id=self.thread_id
            )
            return
        await self._outbox.send_voice(
            self.chat_id, ogg, thread_id=self.thread_id
        )


async def _to_ogg(data: bytes, mime: str) -> bytes:
    suffix = ".mp3" if mime == "audio/mpeg" else ".wav"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"src{suffix}"
        dst = Path(tmp) / "out.ogg"
        src.write_bytes(data)
        proc = await asyncio.create_subprocess_exec(
            get_settings().ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-c:a",
            "libopus",
            str(dst),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(err.decode("utf-8", "replace")[:300])
        return dst.read_bytes()
