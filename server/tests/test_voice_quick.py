import tempfile
from pathlib import Path

import httpx
from sqlalchemy import select

from concierge.agent.runtime import get_runtime
from concierge.conversations.models import Conversation
from concierge.core import db
from concierge.media.models import Attachment
from concierge.media.pipeline import get_pipeline
from tests.conftest import auth, login, make_user
from tests.test_media import _add_provider, _sine, needs_ffmpeg

# Every test transcodes the uploaded audio — ffmpeg is required.
pytestmark = needs_ffmpeg


def _m4a(tmp: str) -> bytes:
    return _sine(Path(tmp) / "v.m4a", ["-c:a", "aac"])


async def _quick(
    client: httpx.AsyncClient,
    token: str,
    blob: bytes | None,
    name: str = "voice.m4a",
    mime: str = "audio/mp4",
    data: dict | None = None,
) -> httpx.Response:
    files = None if blob is None else {"file": (name, blob, mime)}
    return await client.post(
        "/conversations/voice-quick",
        files=files,
        data=data or {},
        headers=auth(token),
    )


async def test_voice_quick_ok(engine, client: httpx.AsyncClient) -> None:
    """voice -> transcript message + audio part -> run -> chat_fast title."""
    text = "включи свет на кухне и расскажи, что сегодня готовят дома на ужин"
    await _add_provider("stt", {"transcript": text})
    await _add_provider("chat", {"custom_output_text": "сделано"})
    await _add_provider("chat_fast", {"custom_output_text": "Свет и ужин"})
    await make_user("vq", "adult")
    token = await login(client, "vq")
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)

    resp = await _quick(client, token, blob)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "ok"
    assert out["transcript"] == text
    cid, mid = out["conversation_id"], out["message_id"]

    conv = (
        await client.get(f"/conversations/{cid}", headers=auth(token))
    ).json()
    assert conv["kind"] == "personal"
    assert conv["origin"] == "voice_quick"

    await get_runtime().wait_idle()
    msgs = (
        await client.get(f"/conversations/{cid}/messages", headers=auth(token))
    ).json()
    first = msgs[0]
    assert first["id"] == mid
    assert first["author_kind"] == "user"
    assert first["parts"][0] == {
        "type": "text",
        "text": text,
        "transcribed": True,
    }
    audio = first["parts"][1]
    assert audio["type"] == "audio"
    assert audio["transcript"] == text
    assert audio["status"] == "done"
    reply = msgs[-1]
    assert reply["author_kind"] == "assistant"
    text_parts = [p for p in reply["parts"] if p["type"] == "text"]
    assert text_parts[-1]["text"] == "сделано"

    conv = (
        await client.get(f"/conversations/{cid}", headers=auth(token))
    ).json()
    assert conv["title"] == "Свет и ужин"


async def test_voice_quick_short_transcript_title(
    engine, client: httpx.AsyncClient
) -> None:
    """Short requests title themselves from the truncated transcript."""
    await _add_provider("stt", {"transcript": "открой гараж"})
    await _add_provider("chat", {"custom_output_text": "ок"})
    await make_user("sh", "adult")
    token = await login(client, "sh")
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)
    resp = await _quick(client, token, blob)
    assert resp.status_code == 200, resp.text
    cid = resp.json()["conversation_id"]
    conv = (
        await client.get(f"/conversations/{cid}", headers=auth(token))
    ).json()
    assert conv["title"] == "открой гараж"
    await get_runtime().wait_idle()


async def test_voice_quick_empty_transcript(
    engine, client: httpx.AsyncClient
) -> None:
    await _add_provider("stt", {"transcript": ""})
    await make_user("em", "adult")
    token = await login(client, "em")
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)
    resp = await _quick(client, token, blob)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "empty_transcript"
    assert out["conversation_id"] is None
    convs = (await client.get("/conversations", headers=auth(token))).json()
    assert convs == []


async def test_voice_quick_stt_unavailable(
    engine, client: httpx.AsyncClient
) -> None:
    """No STT binding: session keeps the audio + a system notice, no run."""
    await make_user("sf", "adult")
    token = await login(client, "sf")
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)
    resp = await _quick(client, token, blob)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "stt_failed"
    cid = out["conversation_id"]

    await get_runtime().wait_idle()
    msgs = (
        await client.get(f"/conversations/{cid}/messages", headers=auth(token))
    ).json()
    assert len(msgs) == 2
    assert msgs[0]["author_kind"] == "user"
    assert len(msgs[0]["parts"]) == 1
    assert msgs[0]["parts"][0]["type"] == "audio"
    assert msgs[0]["parts"][0]["status"] == "skipped"
    assert msgs[1]["author_kind"] == "system"
    assert "Повторить" in msgs[1]["parts"][0]["text"]


async def test_voice_quick_permission_and_input(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("guest1", "guest")
    await make_user("ad", "adult")
    token_g = await login(client, "guest1")
    token_a = await login(client, "ad")

    denied = await client.post(
        "/conversations/voice-quick",
        files={"file": ("v.m4a", b"x", "audio/mp4")},
        headers=auth(token_g),
    )
    assert denied.status_code == 403

    nothing = await _quick(client, token_a, None)
    assert nothing.status_code == 400

    not_audio = await _quick(client, token_a, b"x", "doc.pdf", "application/pdf")
    assert not_audio.status_code == 400

    async with db.get_sessionmaker()() as session:
        convs = (await session.execute(select(Conversation))).scalars().all()
        assert convs == []


async def test_voice_quick_attachment_id(engine, client: httpx.AsyncClient) -> None:
    await _add_provider("stt", {"transcript": "что в холодильнике"})
    await _add_provider("chat", {"custom_output_text": "ок"})
    await make_user("ai", "adult")
    token = await login(client, "ai")
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)
    up = await client.post(
        "/attachments",
        files={"file": ("v.m4a", blob, "audio/mp4")},
        headers=auth(token),
    )
    assert up.status_code == 201
    att_id = up.json()["id"]

    resp = await _quick(client, token, None, data={"attachment_id": str(att_id)})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "ok"
    async with db.get_sessionmaker()() as session:
        att = await session.get(Attachment, att_id)
        assert att is not None
        assert att.conversation_id == out["conversation_id"]

    await make_user("other", "adult")
    token_o = await login(client, "other")
    stolen = await _quick(client, token_o, None, data={"attachment_id": str(att_id)})
    assert stolen.status_code == 404
    await get_runtime().wait_idle()


async def test_retranscribe(engine, client: httpx.AsyncClient) -> None:
    """STT retry endpoint: skipped -> re-run after binding appears."""
    await make_user("rt", "adult")
    await make_user("nosey", "adult")
    token = await login(client, "rt")
    token_o = await login(client, "nosey")
    with tempfile.TemporaryDirectory() as tmp:
        blob = _sine(Path(tmp) / "v.ogg", ["-c:a", "libopus"])
    up = await client.post(
        "/attachments",
        files={"file": ("v.ogg", blob, "audio/ogg")},
        headers=auth(token),
    )
    att_id = up.json()["id"]
    await get_pipeline().ensure_processed(att_id)
    async with db.get_sessionmaker()() as session:
        att = await session.get(Attachment, att_id)
        assert att is not None and att.derived["status"] == "skipped"

    await _add_provider("stt", {"transcript": "повтор попытки"})
    denied = await client.post(
        f"/attachments/{att_id}/retranscribe", headers=auth(token_o)
    )
    assert denied.status_code == 404
    resp = await client.post(
        f"/attachments/{att_id}/retranscribe", headers=auth(token)
    )
    assert resp.status_code == 200, resp.text
    await get_pipeline().ensure_processed(att_id)
    async with db.get_sessionmaker()() as session:
        att = await session.get(Attachment, att_id)
        assert att is not None
        assert att.derived["status"] == "done"
        assert att.derived["transcript"] == "повтор попытки"


async def test_voice_quick_tts_reply(engine, client: httpx.AsyncClient) -> None:
    """voice_quick.tts_reply -> assistant audio message after the text reply."""
    await _add_provider("stt", {"transcript": "расскажи длинную историю про лес и про поляну"})
    await _add_provider("chat", {"custom_output_text": "вот ответ"})
    await _add_provider("tts", {})
    await make_user("tq", "adult")
    token = await login(client, "tq")
    put = await client.put(
        "/settings/user",
        json={"key": "voice_quick.tts_reply", "value": True},
        headers=auth(token),
    )
    assert put.status_code == 200, put.text

    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)
    resp = await _quick(client, token, blob)
    assert resp.status_code == 200, resp.text
    cid = resp.json()["conversation_id"]
    await get_runtime().wait_idle()

    msgs = (
        await client.get(f"/conversations/{cid}/messages", headers=auth(token))
    ).json()
    assert [m["author_kind"] for m in msgs] == ["user", "assistant", "assistant"]
    tts_part = msgs[-1]["parts"][0]
    assert tts_part["type"] == "audio"
    assert tts_part["tts"] is True
    dl = await client.get(
        f"/attachments/{tts_part['attachment_id']}", headers=auth(token)
    )
    assert dl.status_code == 200
    assert dl.content[:4] == b"RIFF"

    # Without the setting there is no voice reply.
    await client.put(
        "/settings/user",
        json={"key": "voice_quick.tts_reply", "value": False},
        headers=auth(token),
    )
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)
    resp = await _quick(client, token, blob)
    assert resp.json()["status"] == "ok"
    cid2 = resp.json()["conversation_id"]
    await get_runtime().wait_idle()
    msgs2 = (
        await client.get(f"/conversations/{cid2}/messages", headers=auth(token))
    ).json()
    assert [m["author_kind"] for m in msgs2] == ["user", "assistant"]
