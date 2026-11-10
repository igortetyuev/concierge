from collections.abc import AsyncIterator

import httpx
import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm
from sqlalchemy import select

from concierge.conversations import service as conversations
from concierge.conversations.models import Conversation, Message
from concierge.core import db
from concierge.memory import indexer, service, store
from tests.conftest import auth, login, make_user
from tests.test_agent import _add_provider, _run_and_wait


@pytest.fixture
async def qdrant(engine) -> AsyncIterator[AsyncQdrantClient]:
    client = AsyncQdrantClient(":memory:")
    store.set_qdrant(client)
    yield client
    store.set_qdrant(None)
    await client.close()


async def test_memory_isolation_and_family(
    qdrant: AsyncQdrantClient,
) -> None:
    await _add_provider("embedding")
    alice = await make_user("alice", "adult")
    bob = await make_user("bob", "adult")
    async with db.get_sessionmaker()() as session:
        secret = await service.remember(
            session, alice, "пароль от wifi qwerty42", "user"
        )
        family = await service.remember(
            session, alice, "код домофона 1234", "family"
        )

        mine = await service.recall(session, alice, "wifi пароль")
        assert secret.id in [h["id"] for h in mine]

        # Fact of user A is not visible to user B.
        foreign = await service.recall(session, bob, "wifi пароль")
        assert secret.id not in [h["id"] for h in foreign]

        # Family scope is shared.
        shared = await service.recall(session, bob, "домофон код")
        assert family.id in [h["id"] for h in shared]


async def test_forget_removes_item_and_vector(
    qdrant: AsyncQdrantClient,
) -> None:
    await _add_provider("embedding")
    alice = await make_user("alice", "adult")
    async with db.get_sessionmaker()() as session:
        item = await service.remember(
            session, alice, "любимый цвет — фиолетовый", "user"
        )
        assert (await service.recall(session, alice, "цвет"))[0]["id"] == item.id
        assert await service.forget(session, alice, item.id)
        assert await service.recall(session, alice, "цвет") == []
        assert [
            i.text for i in await service.list_items(session, alice)
        ] == []


async def test_payload_filters(qdrant: AsyncQdrantClient) -> None:
    """Qdrant-side access filters: owner_id/scope for memory,
    conversation_id list for messages."""
    embedder_slug = "testmodel"
    dim = 4
    client = store.get_qdrant()
    await store.ensure_collections(client, embedder_slug, dim)

    await store.upsert_memory(
        client, embedder_slug, 1, [1, 0, 0, 0],
        {"memory_id": 1, "scope": "user", "owner_id": 7, "text": "a"},
    )
    await store.upsert_memory(
        client, embedder_slug, 2, [1, 0, 0, 0],
        {"memory_id": 2, "scope": "user", "owner_id": 8, "text": "b"},
    )
    await store.upsert_memory(
        client, embedder_slug, 3, [1, 0, 0, 0],
        {"memory_id": 3, "scope": "family", "owner_id": None, "text": "c"},
    )
    hits = await store.search_memory(
        client, embedder_slug, [1, 0, 0, 0], user_id=7, limit=10
    )
    assert {p.id for p in hits} == {1, 3}

    for mid, cid in ((10, 100), (11, 200), (12, 100)):
        await store.upsert_messages(
            client,
            embedder_slug,
            [
                qm.PointStruct(
                    id=mid,
                    vector=[1, 0, 0, 0],
                    payload={"conversation_id": cid, "text": f"m{mid}"},
                )
            ],
        )
    hits = await store.search_messages(
        client, embedder_slug, [1, 0, 0, 0], conversation_ids=[100], limit=10
    )
    assert {p.id for p in hits} == {10, 12}
    # Membership changes are honored: filter is by current conv list.
    hits = await store.search_messages(
        client,
        embedder_slug,
        [1, 0, 0, 0],
        conversation_ids=[100, 200],
        exclude_conversation_id=100,
        limit=10,
    )
    assert {p.id for p in hits} == {11}


async def test_indexer_and_archived_dialog(
    qdrant: AsyncQdrantClient,
) -> None:
    """Messages flow through the outbox; an archived dialogue stays
    retrievable (and summarized) afterwards."""
    await _add_provider("embedding")
    await _add_provider(
        "chat_fast", params={"custom_output_text": "Ежи и яблоки"}
    )
    alice = await make_user("alice", "adult")
    async with db.get_sessionmaker()() as session:
        conv = await conversations.create_conversation(
            session, alice, "personal", "про ежей", []
        )
        assert conv is not None
        await conversations.post_message(
            session, conv, alice,
            [{"type": "text", "text": "ежи очень любят яблоки осенью"}], None,
        )
        conv_id = conv.id

    assert await indexer.index_pending() == 1
    async with db.get_sessionmaker()() as session:
        msg = (await session.execute(select(Message))).scalar_one()
        assert msg.indexed_at is not None

    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, conv_id)
        assert conv is not None
        await conversations.update_conversation(
            session, conv, status="archived"
        )
    await indexer.archive_and_index(conv_id)

    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, conv_id)
        assert conv is not None and conv.summary == "Ежи и яблоки"

        new_conv = await conversations.create_conversation(
            session, alice, "personal", "", []
        )
        assert new_conv is not None
        block = await service.retrieve_context(
            session, alice, new_conv, "что едят ежи"
        )
        assert block is not None
        assert "яблоки" in block
        assert "Из прошлых сессий" in block


async def test_retrieve_context_excludes_other_users(
    qdrant: AsyncQdrantClient,
) -> None:
    await _add_provider("embedding")
    alice = await make_user("alice", "adult")
    bob = await make_user("bob", "adult")
    async with db.get_sessionmaker()() as session:
        alice_conv = await conversations.create_conversation(
            session, alice, "personal", "", []
        )
        bob_conv = await conversations.create_conversation(
            session, bob, "personal", "", []
        )
        assert alice_conv and bob_conv
        await conversations.post_message(
            session, alice_conv, alice,
            [{"type": "text", "text": "секретный рецепт борща"}], None,
        )
        await conversations.post_message(
            session, bob_conv, bob,
            [{"type": "text", "text": "секретный рецепт компота"}], None,
        )
        bob_conv2 = await conversations.create_conversation(
            session, bob, "personal", "", []
        )
        assert bob_conv2 is not None
        await indexer.index_pending()

        block = await service.retrieve_context(
            session, bob, bob_conv2, "секретный рецепт"
        )
        assert block is not None
        assert "компота" in block
        assert "борща" not in block


async def test_memory_api(client: httpx.AsyncClient) -> None:
    await make_user("alice", "adult")
    await make_user("kiddo", "child")
    token = await login(client, "alice")
    kid_token = await login(client, "kiddo")

    resp = await client.post(
        "/memory",
        json={"text": "нравится чай бергамотный", "scope": "user"},
        headers=auth(token),
    )
    assert resp.status_code == 201
    item_id = resp.json()["id"]

    resp = await client.post(
        "/memory",
        json={
            "text": "бабушкина аллергия на цитрусовые",
            "scope": "family",
        },
        headers=auth(kid_token),
    )
    assert resp.status_code == 403

    resp = await client.post(
        "/memory",
        json={
            "text": "бабушкина аллергия на цитрусовые",
            "scope": "family",
        },
        headers=auth(token),
    )
    assert resp.status_code == 201
    family_id = resp.json()["id"]

    items = (await client.get("/memory", headers=auth(kid_token))).json()
    assert {i["id"] for i in items} == {family_id}

    resp = await client.delete(f"/memory/{item_id}", headers=auth(kid_token))
    assert resp.status_code == 403
    resp = await client.delete(f"/memory/{item_id}", headers=auth(token))
    assert resp.status_code == 200
    resp = await client.delete(f"/memory/{item_id}", headers=auth(token))
    assert resp.status_code == 404


async def test_recall_without_embedding_binding(engine) -> None:
    alice = await make_user("alice", "adult")
    async with db.get_sessionmaker()() as session:
        item = await service.remember(session, alice, "просто заметка", "user")
        assert await service.recall(session, alice, "заметка") == []
        assert [i.id for i in await service.list_items(session, alice)] == [
            item.id
        ]


async def test_run_invokes_retrieval(
    engine, client: httpx.AsyncClient, qdrant: AsyncQdrantClient, monkeypatch
) -> None:
    await _add_provider("chat", params={"custom_output_text": "понял"})
    await _add_provider("embedding")
    alice = await make_user("alice", "adult")
    token = await login(client, "alice")
    async with db.get_sessionmaker()() as session:
        await service.remember(session, alice, "любит тёмный шоколад", "user")

    calls: list[str] = []
    orig = service.retrieve_context

    async def spy(session, user, conv, query):  # type: ignore[no-untyped-def]
        calls.append(query)
        return await orig(session, user, conv, query)

    monkeypatch.setattr(
        "concierge.memory.service.retrieve_context", spy
    )
    resp = await client.post(
        "/conversations",
        json={"kind": "personal", "title": "", "member_ids": []},
        headers=auth(token),
    )
    cid = resp.json()["id"]
    resp = await client.post(
        f"/conversations/{cid}/messages",
        json={"parts": [{"type": "text", "text": "что подарить?"}]},
        headers=auth(token),
    )
    assert resp.status_code == 201
    await _run_and_wait(cid, resp.json()["id"])
    assert calls == ["что подарить?"]
    await indexer.wait_idle()
