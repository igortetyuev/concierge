import httpx
from sqlalchemy import select

from concierge.conversations.models import Conversation, Message
from concierge.core import db
from concierge.media.models import Attachment, Voice, VoiceSample
from tests.conftest import auth, login, make_user


async def _voice(label: str = "Голос 1", centroid=None) -> int:
    async with db.get_sessionmaker()() as session:
        voice = Voice(label=label, centroid=centroid or [], sample_count=0)
        session.add(voice)
        await session.commit()
        return int(voice.id)


async def _attachment(user_id: int, sha: str) -> int:
    async with db.get_sessionmaker()() as session:
        att = Attachment(
            sha256=sha,
            mime="audio/wav",
            size=1,
            original_name=f"{sha}.wav",
            uploaded_by=user_id,
        )
        session.add(att)
        await session.commit()
        return int(att.id)


async def _sample(voice_id: int, att_id: int, emb: list[float], score=0.9) -> int:
    async with db.get_sessionmaker()() as session:
        s = VoiceSample(
            voice_id=voice_id, attachment_id=att_id, embedding=emb, score=score
        )
        session.add(s)
        await session.commit()
        return int(s.id)


async def test_voices_crud(engine, client: httpx.AsyncClient) -> None:
    admin = await make_user("admin", "admin")
    user = await make_user("mom", "adult")
    token = await login(client, "admin")
    vid = await _voice()

    rows = (
        await client.get("/admin/voices", headers=auth(token))
    ).json()
    assert len(rows) == 1
    assert rows[0]["label"] == "Голос 1"
    assert rows[0]["user_id"] is None
    assert rows[0]["sample_count"] == 0

    patched = await client.patch(
        f"/admin/voices/{vid}",
        json={"label": "Мама", "user_id": user.id},
        headers=auth(token),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["label"] == "Мама"
    assert patched.json()["user_id"] == user.id
    assert patched.json()["user_name"] == user.display_name

    unmap = await client.patch(
        f"/admin/voices/{vid}", json={"user_id": None}, headers=auth(token)
    )
    assert unmap.status_code == 200
    assert unmap.json()["user_id"] is None

    bad = await client.patch(
        f"/admin/voices/{vid}", json={"user_id": 99999}, headers=auth(token)
    )
    assert bad.status_code == 404

    # admin.id used only implicitly; a guest can't reach the router at all
    assert admin.role_id is not None
    deleted = await client.delete(f"/admin/voices/{vid}", headers=auth(token))
    assert deleted.status_code == 200
    assert (await client.get("/admin/voices", headers=auth(token))).json() == []


async def test_voices_merge(engine, client: httpx.AsyncClient) -> None:
    await make_user("admin", "admin")
    user = await make_user("u", "adult")
    token = await login(client, "admin")
    v1 = await _voice("Голос 1", centroid=[1.0, 0.0])
    v2 = await _voice("Голос 2", centroid=[0.0, 1.0])
    a1 = await _attachment(user.id, "m1")
    a2 = await _attachment(user.id, "m2")
    a3 = await _attachment(user.id, "m3")
    await _sample(v1, a1, [1.0, 0.0])
    await _sample(v2, a2, [0.0, 1.0])
    await _sample(v2, a3, [0.0, 0.9])

    merged = await client.post(
        f"/admin/voices/{v2}/merge", json={"into_id": v1}, headers=auth(token)
    )
    assert merged.status_code == 200, merged.text

    async with db.get_sessionmaker()() as session:
        assert await session.get(Voice, v2) is None
        voice = await session.get(Voice, v1)
        assert voice is not None
        assert voice.sample_count == 3
        samples = (
            await session.execute(
                select(VoiceSample).where(VoiceSample.voice_id == v1)
            )
        ).scalars()
        assert {s.attachment_id for s in samples} == {a1, a2, a3}
        # centroid = normalized mean of the three embeddings
        assert len(voice.centroid) == 2

    merged_again = await client.post(
        f"/admin/voices/{v2}/merge", json={"into_id": v1}, headers=auth(token)
    )
    assert merged_again.status_code == 404


async def test_voice_samples_move_and_delete(engine, client: httpx.AsyncClient) -> None:
    await make_user("admin", "admin")
    user = await make_user("u", "adult")
    token = await login(client, "admin")
    v1 = await _voice("Голос 1")
    v2 = await _voice("Голос 2")
    a1 = await _attachment(user.id, "d1")
    a2 = await _attachment(user.id, "d2")
    s1 = await _sample(v1, a1, [1.0, 0.0])
    s2 = await _sample(v1, a2, [1.0, 0.1])

    listed = await client.get(
        f"/admin/voices/{v1}/samples", headers=auth(token)
    )
    assert listed.status_code == 200
    assert {s["id"] for s in listed.json()} == {s1, s2}
    assert listed.json()[0]["attachment_name"] == "d1.wav"

    # move s2's attachment to v2 — reuses the stored embedding
    moved = await client.post(
        f"/admin/voices/{v2}/samples",
        json={"attachment_id": a2},
        headers=auth(token),
    )
    assert moved.status_code == 201, moved.text
    assert moved.json()["attachment_id"] == a2

    async with db.get_sessionmaker()() as session:
        s = await session.get(VoiceSample, s2)
        assert s is not None and s.voice_id == v2
        v = await session.get(Voice, v2)
        assert v is not None and v.sample_count == 1
        assert v.centroid  # recomputed

    # delete the moved sample from v2
    deleted = await client.delete(
        f"/admin/voices/{v2}/samples/{s2}", headers=auth(token)
    )
    assert deleted.status_code == 200
    async with db.get_sessionmaker()() as session:
        assert await session.get(VoiceSample, s2) is None
        v = await session.get(Voice, v2)
        assert v is not None and v.sample_count == 0

    # wrong voice -> 404
    assert (
        await client.delete(
            f"/admin/voices/{v2}/samples/{s1}", headers=auth(token)
        )
    ).status_code == 404


async def _message_with_speaker(
    owner_id: int, voice_id: int, att_id: int, author_id: int
) -> int:
    async with db.get_sessionmaker()() as session:
        conv = Conversation(kind="group", title="Гостиная", owner_id=owner_id)
        session.add(conv)
        await session.flush()
        msg = Message(
            conversation_id=conv.id,
            seq=1,
            author_kind="user",
            author_id=author_id,
            parts=[
                {
                    "type": "text",
                    "text": "привет",
                    "speaker": {
                        "voice_id": voice_id,
                        "user_id": None,
                        "name": "Голос 1",
                        "score": 0.5,
                    },
                },
                {
                    "type": "audio",
                    "attachment_id": att_id,
                    "mime": "audio/wav",
                },
            ],
        )
        session.add(msg)
        att = await session.get(Attachment, att_id)
        assert att is not None
        att.derived = {
            "status": "done",
            "speaker": {
                "voice_id": voice_id,
                "user_id": None,
                "name": "Голос 1",
                "score": 0.5,
            },
        }
        await session.commit()
        return int(msg.id)


async def test_voice_relabel_rewrites_stored_speaker(
    engine, client: httpx.AsyncClient
) -> None:
    """PATCH (mapping) and merge rewrite the speaker snapshots kept in
    message parts and attachment derived data."""
    await make_user("admin", "admin")
    user = await make_user("mom", "adult")
    token = await login(client, "admin")
    v1 = await _voice("Голос 1")
    v2 = await _voice("Голос 2")
    att = await _attachment(user.id, "r1")
    msg_id = await _message_with_speaker(user.id, v1, att, author_id=user.id)

    patched = await client.patch(
        f"/admin/voices/{v1}", json={"user_id": user.id}, headers=auth(token)
    )
    assert patched.status_code == 200, patched.text
    async with db.get_sessionmaker()() as session:
        msg = await session.get(Message, msg_id)
        assert msg is not None
        sp = msg.parts[0]["speaker"]
        assert sp["voice_id"] == v1
        assert sp["user_id"] == user.id
        assert sp["name"] == user.display_name
        a = await session.get(Attachment, att)
        assert a is not None
        assert a.derived["speaker"]["name"] == user.display_name

    merged = await client.post(
        f"/admin/voices/{v1}/merge", json={"into_id": v2}, headers=auth(token)
    )
    assert merged.status_code == 200, merged.text
    async with db.get_sessionmaker()() as session:
        msg = await session.get(Message, msg_id)
        assert msg is not None
        sp = msg.parts[0]["speaker"]
        assert sp["voice_id"] == v2
        assert sp["name"] == "Голос 2"
        a = await session.get(Attachment, att)
        assert a is not None
        assert a.derived["speaker"]["voice_id"] == v2


async def test_voice_authenticate_retargets_author(
    engine, client: httpx.AsyncClient
) -> None:
    """authenticated+mapped voice: messages carrying its audio are retargeted
    to the voice's owner."""
    await make_user("admin", "admin")
    device = await make_user("wall", "adult")
    owner = await make_user("mom", "adult")
    token = await login(client, "admin")
    v1 = await _voice("Голос 1")
    att = await _attachment(device.id, "a1")
    msg_id = await _message_with_speaker(owner.id, v1, att, author_id=device.id)

    patched = await client.patch(
        f"/admin/voices/{v1}",
        json={"user_id": owner.id, "authenticated": True},
        headers=auth(token),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["authenticated"] is True
    async with db.get_sessionmaker()() as session:
        msg = await session.get(Message, msg_id)
        assert msg is not None
        assert msg.author_id == owner.id

        # flag off -> future audio stays unattributed; history is not reverted
        await client.patch(
            f"/admin/voices/{v1}",
            json={"authenticated": False},
            headers=auth(token),
        )
    async with db.get_sessionmaker()() as session:
        voice = await session.get(Voice, v1)
        assert voice is not None and voice.authenticated is False
