import httpx

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
