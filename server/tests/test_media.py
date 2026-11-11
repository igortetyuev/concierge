import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest

from concierge.agent.models import CapabilityBinding, ModelProvider
from concierge.agent.runtime import AgentRuntime, get_runtime
from concierge.conversations.models import Conversation
from concierge.core import db
from concierge.media import service as media_service
from concierge.media.models import Attachment
from concierge.media.pipeline import get_pipeline
from concierge.media.service import transcribe
from tests.conftest import auth, login, make_user

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")

_png_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c626001000000ffff03000006000"
    "557bfabd40000000049454e44ae426082"
)

_provider_seq = 0


async def _add_provider(capability: str, params: dict) -> None:
    global _provider_seq
    _provider_seq += 1
    async with db.get_sessionmaker()() as session:
        provider = ModelProvider(name=f"mp-{capability}-{_provider_seq}", kind="test")
        session.add(provider)
        await session.flush()
        session.add(
            CapabilityBinding(
                capability=capability,
                provider_id=provider.id,
                model="test-model",
                params=params,
            )
        )
        await session.commit()


async def _conv(client: httpx.AsyncClient, token: str) -> int:
    resp = await client.post(
        "/conversations",
        json={"kind": "personal", "title": "t", "member_ids": []},
        headers=auth(token),
    )
    assert resp.status_code == 201
    return int(resp.json()["id"])


async def _upload(
    client: httpx.AsyncClient,
    token: str,
    name: str,
    data: bytes,
    mime: str,
    conv_id: int | None = None,
) -> httpx.Response:
    data_fields = {} if conv_id is None else {"conversation_id": str(conv_id)}
    return await client.post(
        "/attachments",
        files={"file": (name, data, mime)},
        data=data_fields,
        headers=auth(token),
    )


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
    )


def _sine(path: Path, codec_args: list[str], seconds: int = 1) -> bytes:
    _ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            *codec_args,
            str(path),
        ]
    )
    return path.read_bytes()


def _video(path: Path, seconds: int = 6) -> bytes:
    _ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=330:duration={seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ]
    )
    return path.read_bytes()


def _pdf(pages: int) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i + 1} of {pages} - invoice 55")
    try:
        return doc.tobytes()
    finally:
        doc.close()


async def _wait_attachment(att_id: int) -> Attachment:
    await get_pipeline().ensure_processed(att_id)
    async with db.get_sessionmaker()() as session:
        att = await session.get(Attachment, att_id)
        assert att is not None
        return att


async def test_upload_dedup_and_download(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("u1", "adult")
    token = await login(client, "u1")
    cid = await _conv(client, token)

    resp = await _upload(client, token, "note.txt", b"hello blob", "text/plain", cid)
    assert resp.status_code == 201, resp.text
    att = resp.json()
    assert att["sha256"] and att["name"] == "note.txt"
    assert att["kind"] == "document"

    again = await _upload(client, token, "note.txt", b"hello blob", "text/plain", cid)
    assert again.status_code == 200
    assert again.json()["id"] == att["id"]

    dl = await client.get(f"/attachments/{att['id']}", headers=auth(token))
    assert dl.status_code == 200
    assert dl.content == b"hello blob"
    assert media_service.blob_path(att["sha256"]).exists()


async def test_upload_limit_and_access(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("me", "adult")
    await make_user("guest1", "guest")
    await make_user("other", "adult")
    token = await login(client, "me")
    token_g = await login(client, "guest1")
    token_o = await login(client, "other")
    cid = await _conv(client, token)

    over = await _upload(
        client, token_g, "big.bin", b"x" * (10 * 1024 * 1024 + 1), "application/octet-stream"
    )
    assert over.status_code == 413

    resp = await _upload(client, token, "n.txt", b"secret", "text/plain", cid)
    att_id = resp.json()["id"]
    denied = await client.get(f"/attachments/{att_id}", headers=auth(token_o))
    assert denied.status_code == 404
    missing_conv = await client.post(
        "/attachments",
        files={"file": ("x.txt", b"x", "text/plain")},
        data={"conversation_id": "999999"},
        headers=auth(token_o),
    )
    assert missing_conv.status_code == 404


async def test_pdf_30_pages(engine, client: httpx.AsyncClient) -> None:
    await make_user("doc", "adult")
    token = await login(client, "doc")
    resp = await _upload(client, token, "inv.pdf", _pdf(30), "application/pdf")
    assert resp.status_code == 201, resp.text
    att = await _wait_attachment(resp.json()["id"])
    assert att.derived["status"] == "done"
    assert att.derived["pages"] == 30
    assert "Page 30 of 30" in att.derived["text"]
    assert "invoice 55" in att.derived["text"]


async def test_image_caption(engine, client: httpx.AsyncClient) -> None:
    await _add_provider("vision", {"custom_output_text": "кот на подоконнике"})
    await make_user("pic", "adult")
    token = await login(client, "pic")
    resp = await _upload(client, token, "cat.png", _png_1x1, "image/png")
    assert resp.status_code == 201
    att = await _wait_attachment(resp.json()["id"])
    assert att.derived["status"] == "done"
    assert att.derived["caption"] == "кот на подоконнике"


@needs_ffmpeg
async def test_ogg_opus_transcript(
    engine, client: httpx.AsyncClient
) -> None:
    await _add_provider("stt", {"transcript": "проверка микрофона"})
    await make_user("v", "adult")
    token = await login(client, "v")
    with tempfile.TemporaryDirectory() as tmp:
        blob = _sine(Path(tmp) / "voice.ogg", ["-c:a", "libopus"])
    resp = await _upload(client, token, "voice.ogg", blob, "audio/ogg")
    assert resp.status_code == 201
    att = await _wait_attachment(resp.json()["id"])
    assert att.derived["status"] == "done", att.derived
    assert att.derived["transcript"] == "проверка микрофона"
    assert att.derived["duration_s"] is not None


@needs_ffmpeg
async def test_m4a_transcript(engine, client: httpx.AsyncClient) -> None:
    await _add_provider("stt", {"transcript": "записка домой"})
    await make_user("m4", "adult")
    token = await login(client, "m4")
    with tempfile.TemporaryDirectory() as tmp:
        blob = _sine(Path(tmp) / "voice.m4a", ["-c:a", "aac"])
    resp = await _upload(client, token, "voice.m4a", blob, "audio/mp4")
    assert resp.status_code == 201
    att = await _wait_attachment(resp.json()["id"])
    assert att.derived["status"] == "done", att.derived
    assert att.derived["transcript"] == "записка домой"


@needs_ffmpeg
async def test_transcribe_service(engine, client: httpx.AsyncClient) -> None:
    """Public transcribe(attachment_id) — used by plans 7 (voice-quick) and 8."""
    await _add_provider("stt", {"transcript": "гараж ключи"})
    await make_user("svc", "adult")
    token = await login(client, "svc")
    with tempfile.TemporaryDirectory() as tmp:
        blob = _sine(Path(tmp) / "a.m4a", ["-c:a", "aac"])
    resp = await _upload(client, token, "a.m4a", blob, "audio/mp4")
    text = await transcribe(resp.json()["id"])
    assert text == "гараж ключи"
    assert await transcribe(424242) == ""


@needs_ffmpeg
async def test_video_pipeline(engine, client: httpx.AsyncClient) -> None:
    await _add_provider("stt", {"transcript": "смотри на экран"})
    await _add_provider("vision", {"custom_output_text": "движение точек"})
    await make_user("vid", "adult")
    token = await login(client, "vid")
    with tempfile.TemporaryDirectory() as tmp:
        blob = _video(Path(tmp) / "clip.mp4")
    resp = await _upload(client, token, "clip.mp4", blob, "video/mp4")
    assert resp.status_code == 201
    att = await _wait_attachment(resp.json()["id"])
    assert att.derived["status"] == "done", att.derived
    assert att.derived["transcript"] == "смотри на экран"
    assert att.derived["caption"] == "движение точек"


@needs_ffmpeg
async def test_voice_message_run_uses_transcript(
    engine, client: httpx.AsyncClient
) -> None:
    """голосовое → транскрипт в сообщении, ответ учитывает."""
    await _add_provider("stt", {"transcript": "когда встреча бухгалтера"})
    await _add_provider("chat", {"custom_output_text": "ок"})
    await make_user("vv", "adult")
    token = await login(client, "vv")
    cid = await _conv(client, token)
    with tempfile.TemporaryDirectory() as tmp:
        blob = _sine(Path(tmp) / "v.ogg", ["-c:a", "libopus"])
    up = await _upload(client, token, "v.ogg", blob, "audio/ogg")
    att_id = up.json()["id"]

    resp = await client.post(
        f"/conversations/{cid}/messages",
        json={"parts": [{"type": "audio", "attachment_id": att_id}]},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    msg = resp.json()
    
    # Expanded payload carries the pipeline result once STT landed (the run
    # waited for the pipeline before composing the prompt).
    await get_runtime().wait_idle()
    msgs = (
        await client.get(f"/conversations/{cid}/messages", headers=auth(token))
    ).json()
    expanded = next(m for m in msgs if m["id"] == msg["id"])
    audio = expanded["parts"][0]
    assert audio["transcript"] == "когда встреча бухгалтера"
    assert audio["status"] == "done"
    assert audio["name"] == "v.ogg"

    # The reply run consumed the transcript: the runtime resolved the part.
    async with db.get_sessionmaker()() as session:
        att = await session.get(Attachment, att_id)
        assert att is not None and att.conversation_id == cid
    content = AgentRuntime._content(
        [{"type": "audio", "attachment_id": att_id}],
        Conversation(kind="personal", title="", owner_id=1),
        {},
        None,
        {att_id: att},
    )
    assert content == "когда встреча бухгалтера"


async def test_kind_of_and_missing(engine) -> None:
    assert media_service.kind_of("audio/ogg", "v.ogg") == "audio"
    assert media_service.kind_of("application/pdf", "d.pdf") == "document"
    assert media_service.kind_of("application/octet-stream", "x.m4a") == "audio"
    assert media_service.kind_of("application/octet-stream", "x.bin") == "other"
    async with db.get_sessionmaker()() as session:
        missing = await session.get(Attachment, 987654)
        assert missing is None
