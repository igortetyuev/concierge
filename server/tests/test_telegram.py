import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from concierge.agent.runtime import get_runtime
from concierge.channels import ChannelEvent, Connection, get_hub
from concierge.channels.telegram import gateway as tg_gateway
from concierge.channels.telegram.format import (
    escape_mdv2,
    md_to_mdv2,
    split_telegram,
)
from concierge.channels.telegram.models import (
    TelegramBinding,
    TelegramLink,
    TelegramLinkCode,
    TelegramMessage,
)
from concierge.channels.telegram.service import (
    InboundMedia,
    InboundMessage,
    TelegramService,
    hash_link_code,
)
from concierge.channels.telegram.sink import TelegramSink
from concierge.conversations.models import (
    Conversation,
    ConversationMember,
    Message,
)
from concierge.core import db, ratelimit
from concierge.core.db import utcnow
from concierge.media.models import Attachment
from concierge.settings import service as settings_service
from concierge.settings.models import SettingScope
from tests.conftest import auth, login, make_user
from tests.test_media import _add_provider, _sine, needs_ffmpeg


class FakeOutbox:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._next = 1

    def _id(self) -> int:
        i = self._next
        self._next += 1
        return i

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int,
        parse_mode: str | None = None,
        reply_markup: list[list[tuple[str, str]]] | None = None,
    ) -> int:
        self.calls.append(
            ("send", chat_id, text, thread_id, parse_mode, reply_markup)
        )
        return self._id()

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> None:
        self.calls.append(("edit", chat_id, message_id, text, parse_mode))

    async def send_document(
        self,
        chat_id: int,
        data: bytes,
        filename: str,
        *,
        thread_id: int,
        caption: str | None = None,
    ) -> int:
        self.calls.append(("document", chat_id, filename, len(data), caption))
        return self._id()

    async def send_voice(self, chat_id: int, data: bytes, *, thread_id: int) -> int:
        self.calls.append(("voice", chat_id, len(data)))
        return self._id()

    async def send_audio(
        self, chat_id: int, data: bytes, filename: str, *, thread_id: int
    ) -> int:
        self.calls.append(("audio", chat_id, filename))
        return self._id()

    async def chat_action(self, chat_id: int, action: str, *, thread_id: int) -> None:
        self.calls.append(("action", chat_id, action, thread_id))

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.calls.append(("answer_cb", callback_id, text))

    def sent_texts(self, chat_id: int | None = None) -> list[str]:
        return [
            c[2]
            for c in self.calls
            if c[0] == "send" and (chat_id is None or c[1] == chat_id)
        ]

    def all_texts(self, chat_id: int | None = None) -> list[str]:
        out = self.sent_texts(chat_id)
        out += [
            c[3]
            for c in self.calls
            if c[0] == "edit" and (chat_id is None or c[1] == chat_id)
        ]
        return out

    def markups(self) -> list[Any]:
        return [c[5] for c in self.calls if c[0] == "send" and c[5]]


@pytest.fixture
async def tg(engine):
    """TelegramService with a fake outbox; binding changes wire real sinks."""
    outbox = FakeOutbox()
    bound: list[tuple[int, int, int | None]] = []
    conns: list[Connection] = []
    hub = get_hub()

    async def on_bind(chat_id: int, thread_id: int, conv_id: int | None) -> None:
        bound.append((chat_id, thread_id, conv_id))
        if conv_id is None:
            return
        sink = TelegramSink(outbox, chat_id, thread_id)
        conn = hub.connect(sink)
        conns.append(conn)
        await hub.subscribe(conn, conv_id)

    service = TelegramService(outbox, on_bind)
    yield SimpleNamespace(service=service, outbox=outbox, bound=bound)
    for conn in conns:
        await hub.disconnect(conn)


def _im(**kw: Any) -> InboundMessage:
    base: dict[str, Any] = {
        "chat_id": 100,
        "message_id": 1,
        "tg_user_id": 500,
        "is_private": True,
        "text": "привет",
    }
    base.update(kw)
    return InboundMessage(**base)


async def _link(user_id: int, tg_id: int = 500, username: str = "tguser") -> None:
    async with db.get_sessionmaker()() as session:
        session.add(
            TelegramLink(
                user_id=user_id,
                telegram_user_id=tg_id,
                telegram_username=username,
            )
        )
        await session.commit()


async def test_pairing_and_rate_limit(
    engine, client: httpx.AsyncClient, tg
) -> None:
    await make_user("pa", "adult")
    token = await login(client, "pa")
    resp = await client.post("/telegram/link-code", headers=auth(token))
    assert resp.status_code == 200, resp.text
    code = resp.json()["code"]

    # Bad code -> instruction, no link.
    await tg.service.handle_message(
        _im(text="/start nope", command="start", command_arg="nope", message_id=1)
    )
    assert "привязать" in tg.outbox.sent_texts()[-1]

    # Good code -> link + welcome.
    await tg.service.handle_message(
        _im(text=f"/start {code}", command="start", command_arg=code, message_id=2)
    )
    assert "привязан" in tg.outbox.sent_texts()[-1]
    async with db.get_sessionmaker()() as session:
        link = (
            await session.execute(
                select(TelegramLink).where(TelegramLink.telegram_user_id == 500)
            )
        ).scalar_one_or_none()
        assert link is not None and link.telegram_username is None

    # Expired code -> instruction.
    async with db.get_sessionmaker()() as session:
        session.add(
            TelegramLinkCode(
                code_hash=hash_link_code("old"),
                user_id=link.user_id,
                expires_at=utcnow().replace(year=2000),
            )
        )
        await session.commit()
    await tg.service.handle_message(
        _im(
            text="/start old",
            command="start",
            command_arg="old",
            tg_user_id=777,
            message_id=3,
        )
    )
    assert "привязать" in tg.outbox.sent_texts()[-1]

    # Rate limit: after the instruction above, further messages are silent.
    sends_before = len(tg.outbox.sent_texts())
    await tg.service.handle_message(_im(tg_user_id=777, message_id=4))
    await tg.service.handle_message(_im(tg_user_id=777, message_id=5))
    assert len(tg.outbox.sent_texts()) == sends_before


async def test_pairing_brute_force_limit(engine, client, tg) -> None:
    await make_user("pb", "adult")
    token = await login(client, "pb")
    code = (await client.post("/telegram/link-code", headers=auth(token))).json()[
        "code"
    ]

    # TG_PAIR_LIMIT bad codes, then even a valid code is refused.
    for i in range(ratelimit.TG_PAIR_LIMIT):
        await tg.service.handle_message(
            _im(
                text=f"/start bad{i}",
                command="start",
                command_arg=f"bad{i}",
                tg_user_id=999,
                message_id=100 + i,
            )
        )
    await tg.service.handle_message(
        _im(
            text=f"/start {code}",
            command="start",
            command_arg=code,
            tg_user_id=999,
            message_id=200,
        )
    )
    async with db.get_sessionmaker()() as session:
        link = (
            await session.execute(
                select(TelegramLink).where(TelegramLink.telegram_user_id == 999)
            )
        ).scalar_one_or_none()
        assert link is None


async def test_dm_text_run_and_dedup(
    engine, client: httpx.AsyncClient, tg
) -> None:
    user = await make_user("dm", "adult")
    await _link(user.id)
    await _add_provider("chat", {"custom_output_text": "ответ бота"})

    await tg.service.handle_message(_im(text="привет бот", message_id=10))
    await get_runtime().wait_idle()
    assert "ответ бота" in tg.outbox.all_texts(100)

    async with db.get_sessionmaker()() as session:
        convs = (await session.execute(select(Conversation))).scalars().all()
        assert len(convs) == 1
        conv = convs[0]
        assert conv.kind == "personal" and conv.origin == "telegram"
        count = (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == conv.id)
            )
        ).scalar_one()
        assert count == 2  # user + assistant
        binding = await session.get(TelegramBinding, (100, 0))
        assert binding is not None and binding.kind == "dm_current"

    # Same telegram message delivered again -> no new row, no new reply.
    await tg.service.handle_message(_im(text="привет бот", message_id=10))
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == conv.id)
            )
        ).scalar_one()
        assert count == 2


@needs_ffmpeg
async def test_dm_voice_quick(engine, tg) -> None:
    user = await make_user("vq", "adult")
    await _link(user.id)
    await _add_provider("stt", {"transcript": "скажи погоду"})
    await _add_provider("chat", {"custom_output_text": "ясно"})
    with tempfile.TemporaryDirectory() as tmp:
        blob = _sine(Path(tmp) / "v.ogg", ["-c:a", "libopus"])
    await tg.service.handle_message(
        _im(
            text="",
            message_id=20,
            media=InboundMedia(
                kind="audio", name="v.ogg", mime="audio/ogg", data=blob
            ),
        )
    )
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        msg = (
            await session.execute(select(Message).order_by(Message.seq))
        ).scalars().first()
        assert msg is not None
        assert msg.parts[0]["type"] == "text"
        assert msg.parts[0]["text"] == "скажи погоду"
        assert msg.parts[0]["transcribed"] is True
        assert msg.parts[1]["type"] == "audio"
    assert "ясно" in tg.outbox.all_texts(100)


async def test_topic_group_routing(engine, tg) -> None:
    u1 = await make_user("m1", "adult")
    u2 = await make_user("m2", "adult")
    await _link(u1.id, 500)
    await _link(u2.id, 600, "second")
    await _add_provider("chat", {"custom_output_text": "всем привет"})

    # First message in a forum topic creates a group conv; no mention -> no run.
    await tg.service.handle_message(
        _im(
            chat_id=-100,
            tg_user_id=500,
            is_private=False,
            is_forum_topic=True,
            thread_id=42,
            text="обсуждаем дачу",
            message_id=1,
            topic_title="Дача",
        )
    )
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        conv = (
            await session.execute(select(Conversation))
        ).scalar_one()
        assert conv.kind == "group" and conv.reply_mode == "mention"
        assert conv.title == "Дача" and conv.origin == "telegram"
        msgs = (
            await session.execute(
                select(Message).where(Message.conversation_id == conv.id)
            )
        ).scalars().all()
        assert len(msgs) == 1  # stored, not answered
        binding = await session.get(TelegramBinding, (-100, 42))
        assert binding is not None and binding.kind == "topic"
    assert "всем привет" not in tg.outbox.all_texts(-100)

    # Unlinked sender is ignored entirely.
    await tg.service.handle_message(
        _im(
            chat_id=-100,
            tg_user_id=999,
            is_private=False,
            is_forum_topic=True,
            thread_id=42,
            text="я чужой",
            message_id=2,
        )
    )

    # Second linked sender joins the conv; @mention triggers a run.
    await tg.service.handle_message(
        _im(
            chat_id=-100,
            tg_user_id=600,
            is_private=False,
            is_forum_topic=True,
            thread_id=42,
            text="@testbot расскажи новости",
            mentioned_bot=True,
            message_id=3,
        )
    )
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        members = (
            await session.execute(
                select(ConversationMember.user_id).where(
                    ConversationMember.conversation_id == conv.id
                )
            )
        ).scalars().all()
        assert set(members) == {u1.id, u2.id}
        msgs = (
            await session.execute(
                select(Message).where(Message.conversation_id == conv.id)
            )
        ).scalars().all()
        assert len(msgs) == 3  # m1, m2 (mention), assistant
        mentioned = msgs[1]
        assert {"type": "mention"} in mentioned.parts
    assert "всем привет" in tg.outbox.all_texts(-100)


async def test_plain_group_binding(engine, tg) -> None:
    user = await make_user("pg", "adult")
    await _link(user.id)
    await tg.service.handle_message(
        _im(
            chat_id=-200,
            is_private=False,
            is_forum_topic=False,
            thread_id=5,
            text="группа без топиков",
            message_id=1,
        )
    )
    async with db.get_sessionmaker()() as session:
        binding = await session.get(TelegramBinding, (-200, 0))
        assert binding is not None and binding.kind == "group"


async def test_bound_group_stays_group_with_one_member(
    engine, tg, client: httpx.AsyncClient
) -> None:
    """A telegram-bound group keeps kind=group at a single linked member —
    unlinked chat participants still write there."""
    u1 = await make_user("tg1", "adult")
    u2 = await make_user("tg2", "adult")
    await _link(u1.id, 510)
    await _link(u2.id, 520, "second")
    token = await login(client, "tg1")

    await tg.service.handle_message(
        _im(chat_id=-300, tg_user_id=510, is_private=False, message_id=1)
    )
    await tg.service.handle_message(
        _im(chat_id=-300, tg_user_id=520, is_private=False, message_id=2)
    )
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        conv = (await session.execute(select(Conversation))).scalar_one()
        assert conv.kind == "group"

    resp = await client.delete(
        f"/conversations/{conv.id}/members/{u2.id}", headers=auth(token)
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "group"
    assert resp.json()["reply_mode"] == "mention"


async def test_web_mirror_and_echo_suppression(
    engine, client: httpx.AsyncClient, tg
) -> None:
    user = await make_user("wm", "adult")
    await _link(user.id)
    token = await login(client, "wm")
    await _add_provider("chat", {"custom_output_text": "ок"})
    await tg.service.handle_message(_im(text="первое", message_id=30))
    await get_runtime().wait_idle()
    sends = tg.outbox.sent_texts(100)
    assert "первое" not in sends  # echo suppressed

    async with db.get_sessionmaker()() as session:
        conv = (await session.execute(select(Conversation))).scalar_one()
    resp = await client.post(
        f"/conversations/{conv.id}/messages",
        json={"parts": [{"type": "text", "text": "из веба"}]},
        headers=auth(token),
    )
    assert resp.status_code == 201
    assert "Wm: из веба" in tg.outbox.sent_texts(100)


async def test_streaming_edits_and_final(engine, tg) -> None:
    user = await make_user("st", "adult")
    await _link(user.id)
    await _add_provider("chat", {"custom_output_text": "финал"})
    await tg.service.handle_message(_im(text="вопрос", message_id=40))
    await get_runtime().wait_idle()

    actions = [c for c in tg.outbox.calls if c[0] == "action"]
    assert ("action", 100, "typing", 0) in actions
    edits = [c for c in tg.outbox.calls if c[0] == "edit"]
    assert any(c[4] == "MarkdownV2" or "финал" in c[3] for c in edits)
    async with db.get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(TelegramMessage).where(TelegramMessage.chat_id == 100)
            )
        ).scalars().all()
        assert rows  # inbound + outbound recorded


async def test_commands(engine, tg) -> None:
    user = await make_user("cmd", "adult")
    await _link(user.id)
    await _add_provider("chat", {"custom_output_text": "ок"})

    await tg.service.handle_message(
        _im(text="/new", command="new", message_id=50)
    )
    assert "Новая сессия" in tg.outbox.sent_texts()[-1]
    await tg.service.handle_message(
        _im(text="/sessions", command="sessions", message_id=51)
    )
    markups = tg.outbox.markups()
    assert markups and markups[-1][0][0][1].startswith("sel:")
    conv_id = int(markups[-1][0][0][1][4:])

    await tg.service.handle_callback("cb1", 100, 500, f"sel:{conv_id}")
    assert ("answer_cb", "cb1", "Готово") in tg.outbox.calls

    await tg.service.handle_message(
        _im(text="/voice on", command="voice", command_arg="on", message_id=52)
    )
    async with db.get_sessionmaker()() as session:
        u = await session.get(type(user), user.id)
        mode = await settings_service.get_value(
            session, u, "telegram.voice_reply", "off"
        )
        assert mode == "always"

    await tg.service.handle_message(
        _im(text="/archive", command="archive", message_id=53)
    )
    assert "архивирована" in tg.outbox.sent_texts()[-1]
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, conv_id)
        assert conv is not None and conv.status == "archived"
        assert await session.get(TelegramBinding, (100, 0)) is None
    assert (100, 0, None) in tg.bound

    # In a group /archive is refused.
    await tg.service.handle_message(
        _im(
            text="/archive",
            command="archive",
            is_private=False,
            chat_id=-300,
            message_id=54,
        )
    )
    assert "недоступна в группе" in tg.outbox.sent_texts(-300)[-1]


@needs_ffmpeg
async def test_voice_reply_always(engine, tg) -> None:
    user = await make_user("vr", "adult")
    await _link(user.id)
    await _add_provider("chat", {"custom_output_text": "слушай"})
    await _add_provider("tts", {})
    async with db.get_sessionmaker()() as session:
        u = await session.get(type(user), user.id)
        await settings_service.set_value(
            session, SettingScope.user, u.id, "telegram.voice_reply", "always", u.id
        )
    await tg.service.handle_message(_im(text="поговори", message_id=60))
    await get_runtime().wait_idle()
    voices = [c for c in tg.outbox.calls if c[0] == "voice"]
    assert voices and voices[0][1] == 100


async def test_presence_filter(engine, tg) -> None:
    class RecSink:
        def __init__(self, uid: int) -> None:
            self.user_id = uid
            self.events: list[ChannelEvent] = []

        async def emit(self, event: ChannelEvent) -> None:
            self.events.append(event)

    hub = get_hub()
    rec = RecSink(1)
    conn_rec = hub.connect(rec)
    await hub.subscribe(conn_rec, 555)
    rec.events.clear()

    sink = TelegramSink(tg.outbox, 100, 0)
    conn_tg = hub.connect(sink)
    await hub.subscribe(conn_tg, 555)
    assert hub.online_users(555) == {1}
    assert not any(e.type == "presence" for e in rec.events)
    await hub.disconnect(conn_tg)
    assert not any(e.type == "presence" for e in rec.events)
    await hub.disconnect(conn_rec)


async def test_get_global_unwraps_secret(engine) -> None:
    async with db.get_sessionmaker()() as session:
        await settings_service.set_value(
            session,
            SettingScope.global_,
            0,
            "telegram.bot_token",
            {"secret": "tok123"},
            None,
        )
        value = await settings_service.get_global(session, "telegram.bot_token")
        assert value == "tok123"
        assert await settings_service.get_global(session, "missing", "d") == "d"


async def test_supervisor_toggle(engine, monkeypatch) -> None:
    events: list[str] = []

    class FakeGateway:
        running = False

        async def start(self, token: str) -> None:
            events.append(f"start:{token}")
            self.running = True

        async def stop(self) -> None:
            events.append("stop")
            self.running = False

    monkeypatch.setattr(tg_gateway, "_make_gateway", lambda: FakeGateway())
    async with db.get_sessionmaker()() as session:
        await settings_service.set_value(
            session, SettingScope.global_, 0, "telegram.enabled", True, None
        )
        await settings_service.set_value(
            session,
            SettingScope.global_,
            0,
            "telegram.bot_token",
            {"secret": "T1"},
            None,
        )
    await tg_gateway._tick()
    assert events == ["start:T1"]

    # Same settings -> no restart.
    await tg_gateway._tick()
    assert events == ["start:T1"]

    # Token change -> restart; disable -> stop.
    async with db.get_sessionmaker()() as session:
        await settings_service.set_value(
            session,
            SettingScope.global_,
            0,
            "telegram.bot_token",
            {"secret": "T2"},
            None,
        )
    await tg_gateway._tick()
    assert events[-2:] == ["stop", "start:T2"]
    async with db.get_sessionmaker()() as session:
        await settings_service.set_value(
            session, SettingScope.global_, 0, "telegram.enabled", False, None
        )
    await tg_gateway._tick()
    assert events[-1] == "stop"
    await tg_gateway.stop_supervisor()


async def test_format_helpers() -> None:
    assert escape_mdv2("a.b!") == "a\\.b\\!"
    assert md_to_mdv2("**жирный** и `x=1`") == "*жирный* и `x=1`"
    assert md_to_mdv2("[линк](https://a.b)") == "[линк](https://a.b)"
    parts = split_telegram("x" * 5000)
    assert len(parts) == 2 and len(parts[0]) == 4096
    lines = split_telegram("a\nb\nc")
    assert lines == ["a\nb\nc"]


async def test_api_link_and_admin(
    engine, client: httpx.AsyncClient, tg
) -> None:
    await make_user("ad", "admin")
    guest = await make_user("gu", "guest")
    user = await make_user("lk", "adult")
    token_ad = await login(client, "ad")
    token_g = await login(client, "gu")
    token = await login(client, "lk")

    denied = await client.post("/telegram/link-code", headers=auth(token_g))
    assert denied.status_code == 403

    resp = await client.post("/telegram/link-code", headers=auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["code"]

    link = await client.get("/telegram/link", headers=auth(token))
    assert link.json()["linked"] is False

    # Admin adds a link, overview reflects it.
    add = await client.post(
        "/admin/telegram/links",
        json={
            "user_id": user.id,
            "telegram_user_id": 500,
            "telegram_username": "lk",
        },
        headers=auth(token_ad),
    )
    assert add.status_code == 201, add.text
    link = await client.get("/telegram/link", headers=auth(token))
    assert link.json()["linked"] is True
    assert link.json()["telegram_user_id"] == 500

    links = await client.get("/admin/telegram/links", headers=auth(token_ad))
    assert any(r["telegram_user_id"] == 500 for r in links.json())
    denied_admin = await client.get(
        "/admin/telegram/links", headers=auth(token)
    )
    assert denied_admin.status_code == 403

    # A binding shows up in the overview after a DM.
    await tg.service.handle_message(_im(text="hi", message_id=70))
    await get_runtime().wait_idle()
    overview = (
        await client.get("/admin/telegram/overview", headers=auth(token_ad))
    ).json()
    assert overview["links"] == 1
    assert overview["running"] is False
    assert overview["bindings"][0]["kind"] == "dm_current"
    assert overview["bindings"][0]["chat_id"] == 100

    # Unlink drops link + dm_current binding.
    delete = await client.delete("/telegram/link", headers=auth(token))
    assert delete.status_code == 200
    link = await client.get("/telegram/link", headers=auth(token))
    assert link.json()["linked"] is False
    async with db.get_sessionmaker()() as session:
        assert await session.get(TelegramBinding, (100, 0)) is None
        assert (
            await session.execute(select(TelegramLink))
        ).scalars().all() == []

    # Admin delete path.
    await client.post(
        "/admin/telegram/links",
        json={"user_id": guest.id, "telegram_user_id": 900},
        headers=auth(token_ad),
    )
    delete = await client.delete(
        f"/admin/telegram/links/{guest.id}", headers=auth(token_ad)
    )
    assert delete.status_code == 200


async def test_inbound_message_record(engine, tg) -> None:
    user = await make_user("rec", "adult")
    await _link(user.id)
    await tg.service.handle_message(_im(text="запись", message_id=80))
    async with db.get_sessionmaker()() as session:
        row = await session.get(TelegramMessage, (100, 80))
        assert row is not None and row.conversation_message_id is not None
        att_count = (
            await session.execute(select(func.count()).select_from(Attachment))
        ).scalar_one()
        assert att_count == 0


@pytest.fixture(autouse=True)
async def _reset_supervisor():
    yield
    tg_gateway._gateway = None
    tg_gateway._gateway_token = None


async def test_sink_swallows_outbox_errors(engine, tg) -> None:
    """A failing edit must not drop the hub connection (or the chat goes
    silent until a rebind)."""
    hub = get_hub()

    class FlakyOutbox(FakeOutbox):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next = True

        async def edit(self, *a: Any, **k: Any) -> None:
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("tg api error")
            await super().edit(*a, **k)

    outbox = FlakyOutbox()
    conn = hub.connect(TelegramSink(outbox, 100, 0))
    await hub.subscribe(conn, 999)
    try:
        await hub.publish(
            ChannelEvent("message.delta", 999, {"run_id": 1, "text": "част"})
        )
        await hub.publish(
            ChannelEvent(
                "message.created",
                999,
                {
                    "message": {
                        "id": 1,
                        "author_kind": "assistant",
                        "parts": [{"type": "text", "text": "полный"}],
                        "run_id": 1,
                        "source_channel": "web",
                    }
                },
            )
        )
        # The failing edit was swallowed; the connection still delivers.
        await hub.publish(
            ChannelEvent(
                "message.created",
                999,
                {
                    "message": {
                        "id": 2,
                        "author_kind": "system",
                        "parts": [{"type": "text", "text": "системное"}],
                        "source_channel": "web",
                    }
                },
            )
        )
        assert "системное" in outbox.sent_texts()
    finally:
        await hub.disconnect(conn)


async def test_mdv2_fallback_sends_raw_text(engine) -> None:
    """On a MarkdownV2 parse failure the fallback sends the raw, unescaped
    chunk (no visible backslashes)."""
    hub = get_hub()

    class Mdv2Hater(FakeOutbox):
        async def send(
            self,
            chat_id: int,
            text: str,
            *,
            thread_id: int,
            parse_mode: str | None = None,
            reply_markup: list[list[tuple[str, str]]] | None = None,
        ) -> int:
            if parse_mode == "MarkdownV2":
                raise RuntimeError("can't parse entities")
            return await super().send(
                chat_id,
                text,
                thread_id=thread_id,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )

    outbox = Mdv2Hater()
    conn = hub.connect(TelegramSink(outbox, 100, 0))
    await hub.subscribe(conn, 999)
    try:
        await hub.publish(
            ChannelEvent(
                "message.created",
                999,
                {
                    "message": {
                        "id": 1,
                        "author_kind": "assistant",
                        "parts": [{"type": "text", "text": "**жирный** текст."}],
                        "run_id": 7,
                        "source_channel": "web",
                    }
                },
            )
        )
        assert "**жирный** текст." in outbox.sent_texts()
    finally:
        await hub.disconnect(conn)


async def test_admin_link_delete_drops_binding(
    engine, client: httpx.AsyncClient, tg
) -> None:
    await make_user("root", "admin")
    user = await make_user("linked", "adult")
    token_ad = await login(client, "root")
    await _link(user.id)
    await tg.service.handle_message(_im(text="hi", message_id=90))
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        assert await session.get(TelegramBinding, (100, 0)) is not None
    resp = await client.delete(
        f"/admin/telegram/links/{user.id}", headers=auth(token_ad)
    )
    assert resp.status_code == 200, resp.text
    async with db.get_sessionmaker()() as session:
        assert await session.get(TelegramBinding, (100, 0)) is None
        assert (
            await session.execute(select(TelegramLink))
        ).scalars().all() == []


@needs_ffmpeg
async def test_stt_failure_notice_after_message(engine, tg) -> None:
    """Failed STT: audio message first, system notice second (seq order)."""
    user = await make_user("stt_fail", "adult")
    await _link(user.id)
    await _add_provider("stt", {"transcript": ""})
    with tempfile.TemporaryDirectory() as tmp:
        blob = _sine(Path(tmp) / "v.ogg", ["-c:a", "libopus"])
    await tg.service.handle_message(
        _im(
            text="",
            message_id=95,
            media=InboundMedia(
                kind="audio", name="v.ogg", mime="audio/ogg", data=blob
            ),
        )
    )
    async with db.get_sessionmaker()() as session:
        msgs = (
            await session.execute(
                select(Message).order_by(Message.seq)
            )
        ).scalars().all()
        assert [m.author_kind for m in msgs] == ["user", "system"]
        assert msgs[0].seq < msgs[1].seq
        assert msgs[0].parts[0]["type"] == "audio"
        assert "распознавания" in msgs[1].parts[0]["text"]
