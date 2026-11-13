import math
import tempfile
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from concierge.agent.models import CapabilityBinding, ModelProvider
from concierge.auth.models import User
from concierge.core import db
from concierge.media import speaker
from concierge.media.models import Attachment, Voice, VoiceSample
from concierge.media.pipeline import get_pipeline
from tests.conftest import auth, login, make_user
from tests.test_media import _sine, needs_ffmpeg


async def _provider(capability: str, params: dict) -> None:
    async with db.get_sessionmaker()() as session:
        provider = ModelProvider(name=f"mp-{capability}", kind="test")
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


async def _speaker_provider(embedding: list[float], **params) -> None:
    await _provider("speaker", {"embedding": embedding, **params})


async def _attachment(user_id: int, sha: str) -> int:
    async with db.get_sessionmaker()() as session:
        att = Attachment(
            sha256=sha,
            mime="audio/wav",
            size=1,
            original_name="a.wav",
            uploaded_by=user_id,
        )
        session.add(att)
        await session.commit()
        return int(att.id)


async def _embed(user: User) -> tuple[list[float], dict]:
    async with db.get_sessionmaker()() as session:
        vec, params = await speaker.embed_audio(session, user, Path("x.wav"))
        assert vec is not None
        return vec, params


async def _assign(att_id: int, vec: list[float], params: dict) -> dict:
    async with db.get_sessionmaker()() as session:
        att = await session.get(Attachment, att_id)
        assert att is not None
        out = await speaker.assign_voice(session, att, vec, params)
        await session.commit()
        return out


async def test_no_binding(engine) -> None:
    user = await make_user("spk0", "adult")
    async with db.get_sessionmaker()() as session:
        vec, params = await speaker.embed_audio(session, user, Path("x.wav"))
    assert vec is None and params == {}


async def test_first_embedding_creates_voice(engine) -> None:
    user = await make_user("spk1", "adult")
    await _speaker_provider([1.0, 0.0, 0.0])
    att_id = await _attachment(user.id, "s1")
    vec, params = await _embed(user)
    out = await _assign(att_id, vec, params)
    assert out["voice_id"] == 1
    assert out["name"] == "Голос 1"
    assert out["user_id"] is None
    async with db.get_sessionmaker()() as session:
        voice = (await session.execute(select(Voice))).scalar_one()
        assert voice.label == "Голос 1"
        assert voice.sample_count == 1
        assert voice.sample_attachment_id == att_id
        assert voice.last_heard_at is not None
        assert pytest.approx(1.0) == math.sqrt(sum(x * x for x in voice.centroid))


async def test_close_embedding_joins_and_learns(engine) -> None:
    user = await make_user("spk2", "adult")
    await _speaker_provider([1.0, 0.0, 0.0])
    att1 = await _attachment(user.id, "s2a")
    att2 = await _attachment(user.id, "s2b")
    _, params = await _embed(user)
    first = await _assign(att1, [1.0, 0.0, 0.0], params)
    second = await _assign(att2, [1.0, 0.05, 0.0], params)
    assert second["voice_id"] == first["voice_id"]
    async with db.get_sessionmaker()() as session:
        voice = await session.get(Voice, first["voice_id"])
        assert voice is not None
        assert voice.sample_count == 2
        # centroid moved toward the second sample
        assert voice.centroid[1] > 0.01


async def test_far_embedding_creates_second_voice(engine) -> None:
    user = await make_user("spk3", "adult")
    await _speaker_provider([1.0, 0.0, 0.0])
    att1 = await _attachment(user.id, "s3a")
    att2 = await _attachment(user.id, "s3b")
    _, params = await _embed(user)
    first = await _assign(att1, [1.0, 0.0, 0.0], params)
    second = await _assign(att2, [0.0, 1.0, 0.0], params)
    assert second["voice_id"] != first["voice_id"]
    assert second["name"] == "Голос 2"


async def test_retranscribe_upserts_sample(engine) -> None:
    user = await make_user("spk4", "adult")
    await _speaker_provider([1.0, 0.0, 0.0])
    att_id = await _attachment(user.id, "s4")
    _, params = await _embed(user)
    await _assign(att_id, [1.0, 0.0, 0.0], params)
    await _assign(att_id, [1.0, 0.02, 0.0], params)
    async with db.get_sessionmaker()() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(VoiceSample)
                .where(VoiceSample.attachment_id == att_id)
            )
        ).scalar_one()
        assert count == 1


async def _wait_done(att_id: int) -> Attachment:
    await get_pipeline().ensure_processed(att_id)
    async with db.get_sessionmaker()() as session:
        att = await session.get(Attachment, att_id)
        assert att is not None
        return att


@needs_ffmpeg
async def test_pipeline_assigns_speaker(engine, client: httpx.AsyncClient) -> None:
    await _provider("stt", {"transcript": "привет"})
    await _speaker_provider([1.0, 0.0, 0.0])
    await make_user("pipe", "adult")
    token = await login(client, "pipe")
    with tempfile.TemporaryDirectory() as tmp:
        blob = _sine(Path(tmp) / "v.ogg", ["-c:a", "libopus"], seconds=2)
    resp = await client.post(
        "/attachments",
        files={"file": ("v.ogg", blob, "audio/ogg")},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    att = await _wait_done(resp.json()["id"])
    assert att.derived["status"] == "done", att.derived
    assert att.derived["speaker"]["voice_id"] == 1
    assert att.derived["speaker"]["name"] == "Голос 1"


@needs_ffmpeg
async def test_pipeline_without_binding(engine, client: httpx.AsyncClient) -> None:
    await _provider("stt", {"transcript": "привет"})
    await make_user("nobind", "adult")
    token = await login(client, "nobind")
    with tempfile.TemporaryDirectory() as tmp:
        blob = _sine(Path(tmp) / "v.ogg", ["-c:a", "libopus"], seconds=2)
    resp = await client.post(
        "/attachments",
        files={"file": ("v.ogg", blob, "audio/ogg")},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    att = await _wait_done(resp.json()["id"])
    assert att.derived["status"] == "done", att.derived
    assert "speaker" not in att.derived


async def test_mapped_voice_uses_display_name(engine) -> None:
    user = await make_user("spk5", "adult")
    await _speaker_provider([1.0, 0.0, 0.0])
    att1 = await _attachment(user.id, "s5a")
    att2 = await _attachment(user.id, "s5b")
    _, params = await _embed(user)
    first = await _assign(att1, [1.0, 0.0, 0.0], params)
    async with db.get_sessionmaker()() as session:
        voice = await session.get(Voice, first["voice_id"])
        assert voice is not None
        voice.user_id = user.id
        await session.commit()
    second = await _assign(att2, [1.0, 0.01, 0.0], params)
    assert second["voice_id"] == first["voice_id"]
    assert second["user_id"] == user.id
    assert second["name"] == user.display_name


async def test_content_uses_speaker_name(engine) -> None:
    """Group prompt: mapped voice's user name replaces the sender's (esp32home)."""
    from concierge.agent.runtime import AgentRuntime
    from concierge.conversations.models import Conversation

    user = await make_user("spkuser", "adult")
    col = await make_user("esp32home", "adult")
    conv = Conversation(kind="group", title="", owner_id=col.id)
    member_names = {col.id: col.display_name}

    parts = [
        {
            "type": "text",
            "text": "включи свет",
            "transcribed": True,
            "speaker": {
                "voice_id": 1,
                "user_id": user.id,
                "name": user.display_name,
                "score": 0.9,
            },
        }
    ]
    content = AgentRuntime._content(parts, conv, member_names, col.id, {})
    assert content == f"{user.display_name}: включи свет"

    # Unmapped voice -> its label; audio part reads speaker from att.derived.
    att = Attachment(
        sha256="s6",
        mime="audio/wav",
        size=1,
        original_name="a.wav",
        uploaded_by=col.id,
        derived={
            "transcript": "привет",
            "speaker": {"voice_id": 2, "user_id": None, "name": "Голос 2"},
        },
    )
    parts2 = [{"type": "audio", "attachment_id": 7}]
    content = AgentRuntime._content(
        parts2, conv, member_names, col.id, {7: att}
    )
    assert content == "Голос 2: привет"

    # No speaker data -> sender's member name as before.
    parts3 = [{"type": "text", "text": "привет"}]
    content = AgentRuntime._content(parts3, conv, member_names, col.id, {})
    assert content == f"{col.display_name}: привет"
