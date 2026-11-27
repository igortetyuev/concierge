import tempfile
from pathlib import Path

import httpx

from concierge.agent.runtime import get_runtime
from concierge.conversations.models import Conversation
from concierge.core import db
from concierge.media import tts
from concierge.media.models import Attachment
from tests.conftest import auth, login, make_user
from tests.test_media import _add_provider, _sine, needs_ffmpeg
from tests.test_voice_message import _conv, _msgs, _voice


def _m4a(tmp: str) -> bytes:
    return _sine(Path(tmp) / "v.m4a", ["-c:a", "aac"])


async def _post(
    client: httpx.AsyncClient, token: str, conv_id: int, text: str
) -> dict:
    resp = await client.post(
        f"/conversations/{conv_id}/messages",
        json={"parts": [{"type": "text", "text": text}]},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_speechify() -> None:
    md = (
        "Заголовок\n\n**Жирный** текст и [ссылка]("
        + "https://x.y/z"
        + ") и `кодом`.\n"
        + "```py\nprint('do not read')\n```\n"
        + "- пункт один\n- пункт два "
        + "https://example.com"
    )
    out = tts.speechify(md)
    assert "print" not in out
    assert "example.com" not in out
    assert "**" not in out
    assert "ссылка" in out
    assert "пункт один" in out
    assert "кодом" in out


def test_attachment_markers_stripped() -> None:
    """Модель иногда повторяет метки вложений из истории — чистим везде."""
    src = "Завтра шесть уроков, освободится к 13:55. [голосовой ответ]"
    assert tts.sanitize_reply_text(src) == (
        "Завтра шесть уроков, освободится к 13:55."
    )
    assert tts.speechify(src) == "Завтра шесть уроков, освободится к 13:55."
    assert tts.speechify("Готово. [reply.wav]") == "Готово."
    multi = "Текст.\n\n[reply.wav]\n\nещё текст."  # noqa: RUF001
    assert tts.sanitize_reply_text(multi) == "Текст.\n\nещё текст."  # noqa: RUF001


async def test_spoken_version_short(engine, client: httpx.AsyncClient) -> None:
    """Short replies pass through without a chat_fast call."""
    await make_user("sv", "adult")
    async with db.get_sessionmaker()() as session:
        from concierge.auth.models import User

        user = await session.get(User, 1)
        out = await tts.spoken_version(session, user, "короткий ответ")
    assert out == "короткий ответ"


async def test_spoken_version_compresses(
    engine, client: httpx.AsyncClient
) -> None:
    """Long replies go through chat_fast; fallback truncates at a sentence."""
    long_text = "Первое предложение. " + "Слово " * 200
    await _add_provider("chat_fast", {"custom_output_text": "суть кратко"})
    await make_user("sc", "adult")
    async with db.get_sessionmaker()() as session:
        from concierge.auth.models import User

        user = await session.get(User, 1)
        out = await tts.spoken_version(session, user, long_text)
    assert out == "суть кратко"


async def test_spoken_version_fallback(
    engine, client: httpx.AsyncClient
) -> None:
    """No chat_fast binding: the text is truncated at a sentence boundary."""
    long_text = "Очередное предложение. " * 100
    await make_user("sf", "adult")
    async with db.get_sessionmaker()() as session:
        from concierge.auth.models import User

        user = await session.get(User, 1)
        out = await tts.spoken_version(session, user, long_text)
    assert out.endswith("Очередное предложение.")
    assert len(out) <= tts.SPEECH_SHORT_MAX


@needs_ffmpeg
async def test_speak_message(engine, client: httpx.AsyncClient) -> None:
    """POST .../speak -> wav attachment fetchable via /attachments/{id}."""
    await _add_provider("chat", {"custom_output_text": "ответ ассистента"})
    await _add_provider("tts", {})
    await make_user("sp", "adult")
    await make_user("out", "adult")
    token = await login(client, "sp")
    token_out = await login(client, "out")
    cid = await _conv(client, token)
    await _post(client, token, cid, "вопрос")
    await get_runtime().wait_idle()
    msgs = await _msgs(client, token, cid)
    reply = msgs[-1]
    assert reply["author_kind"] == "assistant"

    resp = await client.post(
        f"/conversations/{cid}/messages/{reply['id']}/speak",
        headers=auth(token),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    dl = await client.get(
        f"/attachments/{out['attachment_id']}", headers=auth(token)
    )
    assert dl.status_code == 200
    assert dl.content[:4] == b"RIFF"

    denied = await client.post(
        f"/conversations/{cid}/messages/{reply['id']}/speak",
        headers=auth(token_out),
    )
    assert denied.status_code == 404
    missing = await client.post(
        f"/conversations/{cid}/messages/99999/speak", headers=auth(token)
    )
    assert missing.status_code == 404


@needs_ffmpeg
async def test_speak_message_no_tts(engine, client: httpx.AsyncClient) -> None:
    """Without a tts binding the endpoint answers 503."""
    await _add_provider("chat", {"custom_output_text": "ок"})
    await make_user("sn", "adult")
    token = await login(client, "sn")
    cid = await _conv(client, token)
    await _post(client, token, cid, "привет")
    await get_runtime().wait_idle()
    msgs = await _msgs(client, token, cid)
    resp = await client.post(
        f"/conversations/{cid}/messages/{msgs[-1]['id']}/speak",
        headers=auth(token),
    )
    assert resp.status_code == 503


@needs_ffmpeg
async def test_voice_session_tts_reply(
    engine, client: httpx.AsyncClient
) -> None:
    """A voice_session line answers with a tts audio message by default."""
    await _add_provider("stt", {"transcript": "консьерж, включи свет"})
    await _add_provider("chat", {"custom_output_text": "свет включён"})
    await _add_provider("tts", {})
    await make_user("vc", "adult")
    token = await login(client, "vc")
    cid = await _conv(client, token)
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        conv.settings = {"voice_session": True}
        await session.commit()
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)
    resp = await _voice(client, token, cid, blob)
    assert resp.status_code == 200, resp.text
    await get_runtime().wait_idle()

    msgs = await _msgs(client, token, cid)
    assert [m["author_kind"] for m in msgs] == ["user", "assistant", "assistant"]
    part = msgs[-1]["parts"][0]
    assert part["type"] == "audio"
    assert part["tts"] is True
    async with db.get_sessionmaker()() as session:
        att = await session.get(Attachment, part["attachment_id"])
        assert att is not None
        assert att.derived["spoken"] == "свет включён"


@needs_ffmpeg
async def test_voice_session_tts_disabled(
    engine, client: httpx.AsyncClient
) -> None:
    """voice_session.tts_reply=False turns the speaker line replies off."""
    await _add_provider("stt", {"transcript": "консьерж, включи свет"})
    await _add_provider("chat", {"custom_output_text": "ок"})
    await _add_provider("tts", {})
    await make_user("vd", "adult")
    token = await login(client, "vd")
    cid = await _conv(client, token)
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        conv.settings = {"voice_session": True}
        await session.commit()
    put = await client.put(
        "/settings/user",
        json={"key": "voice_session.tts_reply", "value": False},
        headers=auth(token),
    )
    assert put.status_code == 200, put.text
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)
    resp = await _voice(client, token, cid, blob)
    assert resp.status_code == 200, resp.text
    await get_runtime().wait_idle()
    msgs = await _msgs(client, token, cid)
    assert [m["author_kind"] for m in msgs] == ["user", "assistant"]


@needs_ffmpeg
async def test_voice_quick_tts_compressed(
    engine, client: httpx.AsyncClient
) -> None:
    """Long voice_quick replies are compressed via chat_fast for speech."""
    long_reply = "Вот подробный ответ. " + "Детали и детали. " * 60
    await _add_provider("stt", {"transcript": "расскажи подробно про лес"})
    await _add_provider("chat", {"custom_output_text": long_reply})
    await _add_provider("chat_fast", {"custom_output_text": "краткая выжимка"})
    await _add_provider("tts", {})
    await make_user("tq", "adult")
    token = await login(client, "tq")
    await client.put(
        "/settings/user",
        json={"key": "voice_quick.tts_reply", "value": True},
        headers=auth(token),
    )
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)
    resp = await client.post(
        "/conversations/voice-quick",
        files={"file": ("v.m4a", blob, "audio/mp4")},
        headers=auth(token),
    )
    assert resp.status_code == 200, resp.text
    cid = resp.json()["conversation_id"]
    await get_runtime().wait_idle()

    msgs = (
        await client.get(
            f"/conversations/{cid}/messages", headers=auth(token)
        )
    ).json()
    assert [m["author_kind"] for m in msgs] == ["user", "assistant", "assistant"]
    tts_part = msgs[-1]["parts"][0]
    assert tts_part["tts"] is True
    async with db.get_sessionmaker()() as session:
        att = await session.get(Attachment, tts_part["attachment_id"])
        assert att is not None
        assert att.derived["spoken"] == "краткая выжимка"
    # The full answer stays in the text reply.
    assert msgs[-2]["parts"][-1]["text"] == long_reply


def test_spoken_override_extracts_last_text_part() -> None:
    parts = [
        {"type": "text", "text": "первый"},
        {"type": "tool_return", "name": "purchase_query", "content": {}},
        {"type": "text", "text": "визуальный", "spoken": "короткая речь"},
    ]
    assert tts.spoken_override(parts) == "короткая речь"
    assert tts.spoken_override([{"type": "text", "text": "без поля"}]) is None
    assert tts.spoken_override([]) is None


async def test_spoken_override_saved_on_reply_text_part(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    import json

    from pydantic_ai.models.function import DeltaToolCall, DeltaToolCalls

    from concierge.purchases import service as purchase_service
    from tests.test_skill_runtime import _use_function_model

    calls = 0

    async def fn(messages, agent_info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="purchase_query",
                        json_args=json.dumps({"general": True, "status": "all"}),
                        tool_call_id="pq1",
                    )
                }
            )
        else:
            yield "визуальный список ниже"

    await _use_function_model(monkeypatch, fn)
    user = await make_user("tts_override", "adult")
    async with db.get_sessionmaker()() as session:
        added = await purchase_service.add(
            session, user.id, "молоко", 2, "шт", general=True
        )
        assert added.ok is True
    token = await login(client, "tts_override")
    cid = await _conv(client, token)
    await _post(client, token, cid, "покажи покупки")
    await get_runtime().wait_idle()
    msgs = await _msgs(client, token, cid)
    reply = msgs[-1]
    texts = [p for p in reply["parts"] if p["type"] == "text"]
    assert texts[-1]["text"] == "визуальный список ниже"
    assert "осталось купить" in texts[-1]["spoken"]
    assert tts.spoken_override(reply["parts"]) == texts[-1]["spoken"]


async def test_voice_session_reply_uses_spoken_override(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    await make_user("tts_line", "adult")
    token = await login(client, "tts_line")
    cid = await _conv(client, token)
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        conv.settings = {"voice_session": True}
        await session.commit()

    captured: dict[str, str] = {}

    async def fake_synthesize(session, user, text: str):
        captured["text"] = text
        return b"audio", "audio/wav"

    async def fail_compressor(session, user, text: str) -> str:
        raise AssertionError("generic spoken_version must not paraphrase overrides")

    monkeypatch.setattr(tts, "synthesize_speech", fake_synthesize)
    monkeypatch.setattr(tts, "spoken_version", fail_compressor)
    async with db.get_sessionmaker()() as session:
        from concierge.auth.models import User

        user = await session.get(User, 1)
        await get_runtime()._maybe_tts_reply(
            cid, user, "визуальный markdown", spoken_override="точная речь"
        )
    assert captured["text"] == "точная речь"


async def test_manual_speak_uses_saved_spoken_override(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    from concierge.conversations.models import Message

    await make_user("tts_manual", "adult")
    token = await login(client, "tts_manual")
    cid = await _conv(client, token)
    async with db.get_sessionmaker()() as session:
        msg = Message(
            conversation_id=cid,
            seq=1,
            author_kind="assistant",
            author_id=None,
            parts=[
                {
                    "type": "text",
                    "text": "полный markdown",
                    "spoken": "короткая закупочная речь",
                }
            ],
        )
        session.add(msg)
        await session.commit()
        mid = msg.id

    captured: dict[str, str] = {}

    async def fake_synthesize(session, user, text: str):
        captured["text"] = text
        return b"audio", "audio/wav"

    monkeypatch.setattr(tts, "synthesize_speech", fake_synthesize)
    resp = await client.post(
        f"/conversations/{cid}/messages/{mid}/speak", headers=auth(token)
    )
    assert resp.status_code == 200, resp.text
    assert captured["text"] == "короткая закупочная речь"


async def test_telegram_voice_reply_uses_spoken_override(
    engine, monkeypatch
) -> None:
    from concierge.channels.telegram.sink import TelegramSink
    from concierge.conversations import service as conversation_service
    from concierge.conversations.models import Message, Run
    from concierge.settings import service as settings_service
    from concierge.settings.models import SettingScope
    from tests.test_telegram import FakeOutbox

    user = await make_user("tts_tg", "adult")
    async with db.get_sessionmaker()() as session:
        conv = await conversation_service.create_conversation(
            session, user, "personal", "", []
        )
        assert conv is not None
        trigger = Message(
            conversation_id=conv.id,
            seq=1,
            author_kind="user",
            author_id=user.id,
            parts=[{"type": "text", "text": "что купить?"}],
        )
        session.add(trigger)
        await session.flush()
        run = Run(
            conversation_id=conv.id,
            trigger_message_id=trigger.id,
            status="done",
        )
        session.add(run)
        await session.commit()
        run_id, conv_id = run.id, conv.id
        await settings_service.set_value(
            session,
            SettingScope.user,
            user.id,
            "telegram.voice_reply",
            "always",
            user.id,
        )

    captured: dict[str, str] = {}

    async def fake_synthesize(session, user, text: str):
        captured["text"] = text
        return b"audio", "audio/wav"

    async def fail_compressor(session, user, text: str) -> str:
        raise AssertionError("generic spoken_version must not paraphrase overrides")

    async def fail_ogg(data: bytes, mime: str) -> bytes:
        raise RuntimeError("no ffmpeg needed in this unit test")

    monkeypatch.setattr(tts, "synthesize_speech", fake_synthesize)
    monkeypatch.setattr(tts, "spoken_version", fail_compressor)
    monkeypatch.setattr("concierge.channels.telegram.sink._to_ogg", fail_ogg)
    sink = TelegramSink(FakeOutbox(), 100, 0)
    await sink._maybe_voice_reply(
        {
            "conversation_id": conv_id,
            "run_id": run_id,
            "text": "визуальный markdown",
            "parts": [
                {
                    "type": "text",
                    "text": "визуальный markdown",
                    "spoken": "точная телеграм-речь",
                }
            ],
        }
    )
    assert captured["text"] == "точная телеграм-речь"
