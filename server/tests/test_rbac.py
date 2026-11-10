import httpx

from tests.conftest import auth, login, make_user


async def test_child_cannot_write_global_settings(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("kid", "child")
    token = await login(client, "kid")

    put = await client.put(
        "/settings/global",
        json={"key": "foo", "value": "bar"},
        headers=auth(token),
    )
    assert put.status_code == 403

    # but can read global and write own user scope
    assert (await client.get("/settings/global", headers=auth(token))).status_code == 200
    put_user = await client.put(
        "/settings/user",
        json={"key": "foo", "value": "bar"},
        headers=auth(token),
    )
    assert put_user.status_code == 200
    got = await client.get("/settings/user", headers=auth(token))
    assert got.json() == {"foo": "bar"}


async def test_admin_writes_global_settings(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("admin", "admin")
    token = await login(client, "admin")
    put = await client.put(
        "/settings/global",
        json={"key": "foo", "value": "bar"},
        headers=auth(token),
    )
    assert put.status_code == 200
    got = await client.get("/settings/global", headers=auth(token))
    assert got.json() == {"foo": "bar"}


async def test_guest_settings_read_only(engine, client: httpx.AsyncClient) -> None:
    await make_user("guest", "guest")
    token = await login(client, "guest")
    assert (await client.get("/settings/user", headers=auth(token))).status_code == 200
    put = await client.put(
        "/settings/user",
        json={"key": "x", "value": 1},
        headers=auth(token),
    )
    assert put.status_code == 403


async def test_admin_api_forbidden_for_non_admin(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("mom", "adult")
    token = await login(client, "mom")
    for path in ("/admin/users", "/admin/roles", "/admin/providers"):
        resp = await client.get(path, headers=auth(token))
        assert resp.status_code == 403, path


async def test_admin_can_list_users(engine, client: httpx.AsyncClient) -> None:
    await make_user("admin", "admin")
    token = await login(client, "admin")
    resp = await client.get("/admin/users", headers=auth(token))
    assert resp.status_code == 200
    assert [u["username"] for u in resp.json()] == ["admin"]


async def test_unauthenticated(engine, client: httpx.AsyncClient) -> None:
    assert (await client.get("/auth/me")).status_code == 401
    assert (await client.get("/settings/user")).status_code == 401
    assert (await client.get("/admin/users")).status_code == 401
