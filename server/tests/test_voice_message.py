import tempfile
from pathlib import Path

import httpx
from sqlalchemy import select

from concierge.agent.runtime import get_runtime
from concierge.auth.models import User
from concierge.conversations.models import Conversation
from concierge.core import db
from tests.conftest import auth, login, make_user
from tests.test_media import _add_provider, _sine, needs_ffmpeg

# Every test transcodes the uploaded audio — ffmpeg is required.
pytestmark = needs_ffmpeg


def _m4a(tmp: str) -> bytes:
    return _sine(Path(tmp) / "v.m4a", ["-c:a", "aac"])


async def _conv(client: httpx.AsyncClient, token: str) -> int:
    resp = await client.post(
        "/conversations",
        json={"kind": "personal", "title": "", "member_ids": []},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def _voice(
    client: httpx.AsyncClient,
    token: str,
    conv_id: int,
    blob: bytes | None,
    data: dict | None = None,
    name: str = "voice.m4a",
    mime: str = "audio/mp4",
) -> httpx.Response:
    files = None if blob is None else {"file": (name, blob, mime)}
    return await client.post(
        f"/conversations/{conv_id}/voice-message",
        files=files,
        data=data or {},
        headers=auth(token),
    )


async def _msgs(client: httpx.AsyncClient, token: str, conv_id: int) -> list[dict]:
    resp = await client.get(
        f"/conversations/{conv_id}/messages", headers=auth(token)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_voice_message_ok(engine, client: httpx.AsyncClient) -> None:
    """audio -> transcript text part with voice_attachment_id -> run."""
    text = "напомни купить молоко и хлеб по дороге домой"
    await _add_provider("stt", {"transcript": text})
    await _add_provider("chat", {"custom_output_text": "готово"})
    await make_user("vm", "adult")
    token = await login(client, "vm")
    cid = await _conv(client, token)
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)

    resp = await _voice(client, token, cid, blob)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "ok"
    assert out["transcript"] == text
    msg = out["message"]
    assert msg["author_kind"] == "user"
    assert len(msg["parts"]) == 1
    part = msg["parts"][0]
    assert part["type"] == "text"
    assert part["text"] == text
    assert part["transcribed"] is True
    att_id = part["voice_attachment_id"]
    assert isinstance(att_id, int)
    dl = await client.get(f"/attachments/{att_id}", headers=auth(token))
    assert dl.status_code == 200

    await get_runtime().wait_idle()
    msgs = await _msgs(client, token, cid)
    assert msgs[-1]["author_kind"] == "assistant"


async def test_voice_message_speaker_part(
    engine, client: httpx.AsyncClient
) -> None:
    """speaker binding -> the text part carries the voice attribution."""
    text = "напомни сходить за хлебом вечером после работы"
    await _add_provider("stt", {"transcript": text})
    await _add_provider("chat", {"custom_output_text": "ок"})
    await _add_provider("speaker", {"embedding": [1.0, 0.0, 0.0]})
    await make_user("vs", "adult")
    token = await login(client, "vs")
    cid = await _conv(client, token)
    with tempfile.TemporaryDirectory() as tmp:
        blob = _sine(Path(tmp) / "v.m4a", ["-c:a", "aac"], seconds=2)

    resp = await _voice(client, token, cid, blob)
    assert resp.status_code == 200, resp.text
    part = resp.json()["message"]["parts"][0]
    assert part["speaker"]["voice_id"] == 1
    assert part["speaker"]["name"] == "Голос 1"
    await get_runtime().wait_idle()


async def test_voice_message_merges_text_and_attachments(
    engine, client: httpx.AsyncClient
) -> None:
    """Typed draft + staged attachments ride along with the transcript."""
    await _add_provider("stt", {"transcript": "и ещё вот этот файл"})
    await _add_provider("chat", {"custom_output_text": "ок"})
    await make_user("mx", "adult")
    token = await login(client, "mx")
    cid = await _conv(client, token)
    up = await client.post(
        "/attachments",
        files={"file": ("a.txt", b"hello", "text/plain")},
        headers=auth(token),
    )
    assert up.status_code == 201, up.text
    att_id = up.json()["id"]
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)

    resp = await _voice(
        client,
        token,
        cid,
        blob,
        data={"text": "посмотри", "attachment_ids": [str(att_id)]},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "ok"
    parts = out["message"]["parts"]
    assert parts[0]["text"] == "посмотри и ещё вот этот файл"
    assert parts[0]["voice_attachment_id"]
    doc = parts[1]
    assert doc["type"] == "document"
    assert doc["attachment_id"] == att_id
    await get_runtime().wait_idle()


async def test_voice_message_empty_transcript(
    engine, client: httpx.AsyncClient
) -> None:
    """Nothing heard and nothing else to send: no message is posted."""
    await _add_provider("stt", {"transcript": ""})
    await make_user("em", "adult")
    token = await login(client, "em")
    cid = await _conv(client, token)
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)

    resp = await _voice(client, token, cid, blob)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "empty_transcript"
    assert out["message"] is None
    assert await _msgs(client, token, cid) == []


async def test_voice_message_empty_transcript_with_text(
    engine, client: httpx.AsyncClient
) -> None:
    """Empty transcript + typed text: the text still goes out, audio kept."""
    await _add_provider("stt", {"transcript": ""})
    await _add_provider("chat", {"custom_output_text": "ок"})
    await make_user("et", "adult")
    token = await login(client, "et")
    cid = await _conv(client, token)
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)

    resp = await _voice(client, token, cid, blob, data={"text": "просто текст"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "empty_transcript"
    msg = out["message"]
    assert msg["parts"][0] == {"type": "text", "text": "просто текст"}
    assert msg["parts"][1]["type"] == "audio"
    await get_runtime().wait_idle()
    msgs = await _msgs(client, token, cid)
    assert msgs[-1]["author_kind"] == "assistant"


async def test_voice_message_stt_unavailable(
    engine, client: httpx.AsyncClient
) -> None:
    """No STT binding: audio stays playable + system notice; run only with text."""
    await _add_provider("chat", {"custom_output_text": "ок"})
    await make_user("sf", "adult")
    token = await login(client, "sf")
    cid = await _conv(client, token)
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)

    resp = await _voice(client, token, cid, blob)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "stt_failed"
    assert out["message"]["parts"][0]["type"] == "audio"

    await get_runtime().wait_idle()
    msgs = await _msgs(client, token, cid)
    assert [m["author_kind"] for m in msgs] == ["user", "system"]
    assert "Ошибка распознавания" in msgs[1]["parts"][0]["text"]

    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)
    resp = await _voice(client, token, cid, blob, data={"text": "напечатал"})
    out = resp.json()
    assert out["status"] == "stt_failed"
    assert out["message"]["parts"][0] == {"type": "text", "text": "напечатал"}

    await get_runtime().wait_idle()
    msgs = await _msgs(client, token, cid)
    assert [m["author_kind"] for m in msgs] == [
        "user",
        "system",
        "user",
        "system",
        "assistant",
    ]


async def test_voice_message_recreates_inactive_session(
    engine, client: httpx.AsyncClient
) -> None:
    """An inactive `voice_session` conversation spawns a successor with all
    users as members; the message lands there, and the line keeps moving."""
    await _add_provider("stt", {"transcript": "консьерж, включи проектор"})
    await _add_provider("chat", {"custom_output_text": "ок"})
    await make_user("vo", "adult")
    await make_user("vo2", "adult")
    token = await login(client, "vo")
    cid = await _conv(client, token)

    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        conv.settings = {"voice_session": True}
        conv.status = "archived"
        await session.commit()
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)

    resp = await _voice(client, token, cid, blob)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    new_cid = out["conversation_id"]
    assert new_cid != cid
    async with db.get_sessionmaker()() as session:
        clone = await session.get(Conversation, new_cid)
        assert clone is not None
        assert clone.status == "active"
        assert clone.settings["voice_root"] == cid
        member_ids = {m.user_id for m in clone.members}
        all_ids = set(
            (await session.execute(select(User.id))).scalars()
        )
        assert member_ids == all_ids
    msgs = await _msgs(client, token, new_cid)
    assert msgs[0]["parts"][0]["text"] == "консьерж, включи проектор"
    await get_runtime().wait_idle()

    # While the clone is live the same stale id keeps routing to it.
    resp = await _voice(client, token, cid, blob)
    assert resp.status_code == 200, resp.text
    assert resp.json()["conversation_id"] == new_cid
    await get_runtime().wait_idle()

    # Archive the clone too: the line moves on to a third session.
    async with db.get_sessionmaker()() as session:
        clone = await session.get(Conversation, new_cid)
        assert clone is not None
        clone.status = "archived"
        await session.commit()
    resp = await _voice(client, token, cid, blob)
    assert resp.status_code == 200, resp.text
    third_cid = resp.json()["conversation_id"]
    assert third_cid not in (cid, new_cid)
    async with db.get_sessionmaker()() as session:
        third = await session.get(Conversation, third_cid)
        assert third is not None
        assert third.settings["voice_root"] == cid
    await get_runtime().wait_idle()


async def test_voice_message_archived_without_flag(
    engine, client: httpx.AsyncClient
) -> None:
    """A regular archived session still rejects voice posts with 409."""
    await make_user("nf", "adult")
    token = await login(client, "nf")
    cid = await _conv(client, token)
    resp = await client.patch(
        f"/conversations/{cid}",
        json={"status": "archived"},
        headers=auth(token),
    )
    assert resp.status_code == 200, resp.text
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)

    resp = await _voice(client, token, cid, blob)
    assert resp.status_code == 409


async def test_voice_message_membership_and_input(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("own", "adult")
    await make_user("str", "adult")
    token_o = await login(client, "own")
    token_s = await login(client, "str")
    cid = await _conv(client, token_o)
    with tempfile.TemporaryDirectory() as tmp:
        blob = _m4a(tmp)

    resp = await _voice(client, token_s, cid, blob)
    assert resp.status_code == 404

    not_audio = await _voice(
        client, token_o, cid, b"x", name="d.pdf", mime="application/pdf"
    )
    assert not_audio.status_code == 400

    missing = await _voice(
        client, token_o, cid, blob, data={"attachment_ids": ["99999"]}
    )
    assert missing.status_code == 404
