import httpx
from sqlalchemy import select

from concierge.agent.runtime import get_runtime
from concierge.auth.models import Role
from concierge.conversations.models import Conversation, Run
from concierge.core import db
from concierge.memory import indexer as memory_indexer
from tests.conftest import auth, login, make_user


async def _create(
    client: httpx.AsyncClient,
    token: str,
    kind: str = "personal",
    title: str = "",
    member_ids: list[int] | None = None,
) -> dict:
    resp = await client.post(
        "/conversations",
        json={
            "kind": kind,
            "title": title,
            "member_ids": member_ids or [],
        },
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_and_get_conversation(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("alice", "adult")
    token = await login(client, "alice")

    conv = await _create(client, token, title="Личная")
    assert conv["kind"] == "personal"
    assert conv["reply_mode"] == "always"
    assert [m["username"] for m in conv["members"]] == ["alice"]

    got = await client.get(f"/conversations/{conv['id']}", headers=auth(token))
    assert got.status_code == 200
    assert got.json()["title"] == "Личная"

    listing = await client.get("/conversations", headers=auth(token))
    assert [c["id"] for c in listing.json()] == [conv["id"]]


async def test_group_membership_and_visibility(
    engine, client: httpx.AsyncClient
) -> None:
    alice = await make_user("alice", "adult")
    bob = await make_user("bob", "adult")
    carol = await make_user("carol", "adult")
    token_a = await login(client, "alice")
    token_b = await login(client, "bob")
    token_c = await login(client, "carol")

    conv = await _create(
        client, token_a, kind="group", title="Семья", member_ids=[bob.id]
    )
    assert conv["reply_mode"] == "mention"
    assert {m["user_id"] for m in conv["members"]} == {alice.id, bob.id}

    # Member can read, non-member gets 404.
    assert (
        await client.get(f"/conversations/{conv['id']}", headers=auth(token_b))
    ).status_code == 200
    assert (
        await client.get(f"/conversations/{conv['id']}", headers=auth(token_c))
    ).status_code == 404
    assert (await client.get("/conversations", headers=auth(token_c))).json() == []

    # Owner adds carol; non-owner cannot.
    assert (
        await client.post(
            f"/conversations/{conv['id']}/members",
            json={"user_ids": [carol.id]},
            headers=auth(token_b),
        )
    ).status_code == 403
    resp = await client.post(
        f"/conversations/{conv['id']}/members",
        json={"user_ids": [carol.id]},
        headers=auth(token_a),
    )
    assert {m["user_id"] for m in resp.json()["members"]} == {
        alice.id,
        bob.id,
        carol.id,
    }

    # Member can remove self; owner cannot be removed.
    resp = await client.delete(
        f"/conversations/{conv['id']}/members/{carol.id}", headers=auth(token_c)
    )
    assert resp.status_code == 200
    assert (
        await client.delete(
            f"/conversations/{conv['id']}/members/{alice.id}",
            headers=auth(token_a),
        )
    ).status_code == 400


async def test_kind_follows_membership(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("alice", "adult")
    bob = await make_user("bob", "adult")
    token_a = await login(client, "alice")
    token_b = await login(client, "bob")

    # A "group" request without other members creates a personal session.
    solo = await _create(client, token_a, kind="group")
    assert solo["kind"] == "personal"
    assert solo["reply_mode"] == "always"

    conv = await _create(client, token_a)
    assert conv["kind"] == "personal"

    # Adding a member turns a personal session into a group.
    resp = await client.post(
        f"/conversations/{conv['id']}/members",
        json={"user_ids": [bob.id]},
        headers=auth(token_a),
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "group"
    assert resp.json()["reply_mode"] == "mention"
    assert (
        await client.get(f"/conversations/{conv['id']}", headers=auth(token_b))
    ).status_code == 200

    # Back to a single member — the session is personal again.
    resp = await client.delete(
        f"/conversations/{conv['id']}/members/{bob.id}", headers=auth(token_a)
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "personal"
    assert resp.json()["reply_mode"] == "always"


async def test_patch_and_archive(engine, client: httpx.AsyncClient) -> None:
    await make_user("alice", "adult")
    bob = await make_user("bob", "adult")
    token_a = await login(client, "alice")
    token_b = await login(client, "bob")
    conv = await _create(
        client, token_a, kind="group", member_ids=[bob.id]
    )

    # Non-owner member cannot modify.
    assert (
        await client.patch(
            f"/conversations/{conv['id']}",
            json={"title": "x"},
            headers=auth(token_b),
        )
    ).status_code == 403

    resp = await client.patch(
        f"/conversations/{conv['id']}",
        json={"title": "Новое имя", "status": "archived"},
        headers=auth(token_a),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Новое имя"
    assert body["status"] == "archived"
    assert body["archived_at"] is not None

    # Not in default listing, but visible with status filter.
    assert (await client.get("/conversations", headers=auth(token_a))).json() == []
    archived = await client.get(
        "/conversations", params={"status": "archived"}, headers=auth(token_a)
    )
    assert [c["id"] for c in archived.json()] == [conv["id"]]

    # Posting to archived conversation is rejected.
    assert (
        await client.post(
            f"/conversations/{conv['id']}/messages",
            json={"parts": [{"type": "text", "text": "hi"}]},
            headers=auth(token_a),
        )
    ).status_code == 409

    # Unarchive.
    resp = await client.patch(
        f"/conversations/{conv['id']}",
        json={"status": "active"},
        headers=auth(token_a),
    )
    assert resp.json()["status"] == "active"
    assert resp.json()["archived_at"] is None


async def test_messages_paging_and_idempotency(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("alice", "adult")
    token = await login(client, "alice")
    conv = await _create(client, token)

    for i in range(5):
        resp = await client.post(
            f"/conversations/{conv['id']}/messages",
            json={"parts": [{"type": "text", "text": f"msg {i}"}]},
            headers=auth(token),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["seq"] == i + 1

    page = await client.get(
        f"/conversations/{conv['id']}/messages",
        params={"before_seq": 4, "limit": 2},
        headers=auth(token),
    )
    assert [m["seq"] for m in page.json()] == [2, 3]

    # Idempotent replay: same client_msg_id returns the same message with 200.
    first = await client.post(
        f"/conversations/{conv['id']}/messages",
        json={
            "parts": [{"type": "text", "text": "once"}],
            "client_msg_id": "c-1",
        },
        headers=auth(token),
    )
    assert first.status_code == 201
    second = await client.post(
        f"/conversations/{conv['id']}/messages",
        json={
            "parts": [{"type": "text", "text": "once"}],
            "client_msg_id": "c-1",
        },
        headers=auth(token),
    )
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["seq"] == first.json()["seq"]


async def test_mark_read(engine, client: httpx.AsyncClient) -> None:
    alice = await make_user("alice", "adult")
    bob = await make_user("bob", "adult")
    token_a = await login(client, "alice")
    token_b = await login(client, "bob")
    conv = await _create(client, token_a, kind="group", member_ids=[bob.id])

    await client.post(
        f"/conversations/{conv['id']}/messages",
        json={"parts": [{"type": "text", "text": "hi bob"}]},
        headers=auth(token_a),
    )
    resp = await client.post(
        f"/conversations/{conv['id']}/read",
        json={"seq": 1},
        headers=auth(token_b),
    )
    assert resp.json() == {"last_read_seq": 1}

    # Never regresses.
    resp = await client.post(
        f"/conversations/{conv['id']}/read",
        json={"seq": 0},
        headers=auth(token_b),
    )
    assert resp.json() == {"last_read_seq": 1}

    members = await client.get(
        f"/conversations/{conv['id']}/members", headers=auth(token_b)
    )
    by_user = {m["user_id"]: m for m in members.json()}
    assert by_user[bob.id]["last_read_seq"] == 1
    assert by_user[alice.id]["last_read_seq"] == 1


async def test_delete_message(engine, client: httpx.AsyncClient) -> None:
    await make_user("alice", "adult")
    bob = await make_user("bob", "adult")
    token_a = await login(client, "alice")
    token_b = await login(client, "bob")
    conv = await _create(client, token_a, kind="group", member_ids=[bob.id])

    async def post(token: str, text: str) -> int:
        resp = await client.post(
            f"/conversations/{conv['id']}/messages",
            json={"parts": [{"type": "text", "text": text}]},
            headers=auth(token),
        )
        assert resp.status_code == 201, resp.text
        return int(resp.json()["id"])

    msg_a = await post(token_a, "from alice")
    # Each post schedules a background agent run; on the shared sqlite
    # connection its session's ROLLBACK can discard the next request's
    # uncommitted work — drain runs between DB-touching steps.
    await get_runtime().wait_idle()
    msg_b = await post(token_b, "from bob")
    await get_runtime().wait_idle()

    # A run referencing the message must not block its deletion.
    async with db.get_sessionmaker()() as session:
        session.add(
            Run(
                conversation_id=conv["id"],
                trigger_message_id=msg_a,
                status="done",
            )
        )
        await session.commit()

    # A member cannot delete somebody else's message.
    assert (
        await client.delete(
            f"/conversations/{conv['id']}/messages/{msg_a}",
            headers=auth(token_b),
        )
    ).status_code == 403

    # A non-member gets 404.
    await make_user("mallory", "adult")
    token_m = await login(client, "mallory")
    assert (
        await client.delete(
            f"/conversations/{conv['id']}/messages/{msg_a}",
            headers=auth(token_m),
        )
    ).status_code == 404

    # The author deletes their own message.
    resp = await client.delete(
        f"/conversations/{conv['id']}/messages/{msg_b}", headers=auth(token_b)
    )
    assert resp.status_code == 200

    # The owner may delete any message; the run trigger link is cleared.
    resp = await client.delete(
        f"/conversations/{conv['id']}/messages/{msg_a}", headers=auth(token_a)
    )
    assert resp.status_code == 200
    async with db.get_sessionmaker()() as session:
        run = (
            await session.execute(
                select(Run).where(Run.conversation_id == conv["id"])
            )
        ).scalar_one()
        assert run.trigger_message_id is None

    msgs = await client.get(
        f"/conversations/{conv['id']}/messages", headers=auth(token_a)
    )
    assert msgs.json() == []

    # Deleting a missing message is a 404.
    assert (
        await client.delete(
            f"/conversations/{conv['id']}/messages/{msg_a}",
            headers=auth(token_a),
        )
    ).status_code == 404


async def test_delete_conversation_is_soft_and_admin_only(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("alice", "adult")
    bob = await make_user("bob", "adult")
    admin = await make_user("carol", "admin")
    token_a = await login(client, "alice")
    token_b = await login(client, "bob")
    token_adm = await login(client, "carol")
    conv = await _create(
        client, token_a, kind="group", member_ids=[bob.id, admin.id]
    )
    await client.post(
        f"/conversations/{conv['id']}/messages",
        json={"parts": [{"type": "text", "text": "секретное слово"}]},
        headers=auth(token_a),
    )
    # Drain the scheduled run; its session shares the sqlite connection.
    await get_runtime().wait_idle()

    # Only archived sessions can be deleted.
    assert (
        await client.delete(
            f"/conversations/{conv['id']}", headers=auth(token_adm)
        )
    ).status_code == 409

    resp = await client.patch(
        f"/conversations/{conv['id']}",
        json={"status": "archived"},
        headers=auth(token_a),
    )
    assert resp.status_code == 200
    await memory_indexer.wait_idle()

    # Permission-gated: members without delete_conversations get 403.
    assert (
        await client.delete(
            f"/conversations/{conv['id']}", headers=auth(token_b)
        )
    ).status_code == 403
    assert (
        await client.delete(
            f"/conversations/{conv['id']}", headers=auth(token_a)
        )
    ).status_code == 403

    # Grant the permission to adults: a non-owner member still cannot delete.
    async with db.get_sessionmaker()() as session:
        role = (
            await session.execute(select(Role).where(Role.name == "adult"))
        ).scalar_one()
        role.permissions = {**role.permissions, "delete_conversations": True}
        await session.commit()
    assert (
        await client.delete(
            f"/conversations/{conv['id']}", headers=auth(token_b)
        )
    ).status_code == 403

    # The owner with the permission soft-deletes: the row stays.
    resp = await client.delete(
        f"/conversations/{conv['id']}", headers=auth(token_a)
    )
    assert resp.status_code == 200, resp.text
    async with db.get_sessionmaker()() as session:
        row = await session.get(Conversation, conv["id"])
        assert row is not None
        assert row.status == "deleted"
        assert row.archived_at is not None

    # Hidden from regular members, visible to the admin.
    assert (
        await client.get(f"/conversations/{conv['id']}", headers=auth(token_a))
    ).status_code == 404
    assert (
        await client.get(f"/conversations/{conv['id']}", headers=auth(token_b))
    ).status_code == 404
    assert (
        await client.get(f"/conversations/{conv['id']}", headers=auth(token_adm))
    ).status_code == 200
    assert (
        await client.get(
            "/conversations", params={"status": "all"}, headers=auth(token_a)
        )
    ).json() == []
    deleted = await client.get(
        "/conversations", params={"status": "deleted"}, headers=auth(token_adm)
    )
    assert [c["id"] for c in deleted.json()] == [conv["id"]]
    assert (
        await client.get(
            "/conversations", params={"status": "deleted"}, headers=auth(token_a)
        )
    ).status_code == 403

    # Search skips deleted sessions.
    resp = await client.get(
        "/search", params={"q": "секретное"}, headers=auth(token_a)
    )
    assert resp.json() == []

    # Re-deleting and posting are rejected; the admin can restore to archived.
    assert (
        await client.delete(
            f"/conversations/{conv['id']}", headers=auth(token_adm)
        )
    ).status_code == 409
    resp = await client.patch(
        f"/conversations/{conv['id']}",
        json={"status": "archived"},
        headers=auth(token_adm),
    )
    assert resp.json()["status"] == "archived"


async def test_search_finds_archived_conversation(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("alice", "adult")
    bob = await make_user("bob", "adult")
    token_a = await login(client, "alice")
    token_b = await login(client, "bob")
    conv = await _create(client, token_a, kind="group", member_ids=[bob.id])

    await client.post(
        f"/conversations/{conv['id']}/messages",
        json={"parts": [{"type": "text", "text": "купить молоко завтра"}]},
        headers=auth(token_a),
    )
    await client.patch(
        f"/conversations/{conv['id']}",
        json={"status": "archived"},
        headers=auth(token_a),
    )

    # Archived conversation is still searchable for both members...
    resp = await client.get("/search", params={"q": "молоко"}, headers=auth(token_a))
    assert resp.status_code == 200
    hits = resp.json()
    assert len(hits) == 1
    assert hits[0]["conversation_id"] == conv["id"]
    resp = await client.get("/search", params={"q": "молоко"}, headers=auth(token_b))
    assert len(resp.json()) == 1

    # ...but not for non-members.
    await make_user("mallory", "adult")
    token_m = await login(client, "mallory")
    resp = await client.get("/search", params={"q": "молоко"}, headers=auth(token_m))
    assert resp.json() == []
