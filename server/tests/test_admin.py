import httpx

from concierge.agent.service import get_capability_binding
from concierge.core.db import get_sessionmaker
from tests.conftest import auth, login, make_user


async def test_admin_user_crud(engine, client: httpx.AsyncClient) -> None:
    await make_user("admin", "admin")
    token = await login(client, "admin")

    roles = await client.get("/admin/roles", headers=auth(token))
    adult_id = next(r["id"] for r in roles.json() if r["name"] == "adult")

    created = await client.post(
        "/admin/users",
        json={
            "username": "mom",
            "display_name": "Mom",
            "password": "password123",
            "role_id": adult_id,
        },
        headers=auth(token),
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]

    patched = await client.patch(
        f"/admin/users/{user_id}",
        json={"is_active": False},
        headers=auth(token),
    )
    assert patched.status_code == 200
    assert patched.json()["is_active"] is False

    deleted = await client.delete(f"/admin/users/{user_id}", headers=auth(token))
    assert deleted.status_code == 200


async def test_admin_role_crud(engine, client: httpx.AsyncClient) -> None:
    await make_user("admin", "admin")
    token = await login(client, "admin")

    created = await client.post(
        "/admin/roles",
        json={"name": "nanny", "permissions": {"settings": "r"}},
        headers=auth(token),
    )
    assert created.status_code == 201
    role_id = created.json()["id"]

    patched = await client.patch(
        f"/admin/roles/{role_id}",
        json={"permissions": {"*": True}},
        headers=auth(token),
    )
    assert patched.json()["permissions"] == {"*": True}

    # seeded roles are deletable (no users), and admin role has users? admin user
    # holds role admin -> delete must 409
    roles = await client.get("/admin/roles", headers=auth(token))
    admin_role = next(r for r in roles.json() if r["name"] == "admin")
    resp = await client.delete(f"/admin/roles/{admin_role['id']}", headers=auth(token))
    assert resp.status_code == 409

    assert (
        await client.delete(f"/admin/roles/{role_id}", headers=auth(token))
    ).status_code == 200


async def test_capability_binding_change_without_restart(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("admin", "admin")
    token = await login(client, "admin")

    p1 = await client.post(
        "/admin/providers",
        json={"name": "ollama", "kind": "ollama", "base_url": "http://x:11434/v1"},
        headers=auth(token),
    )
    assert p1.status_code == 201
    p2 = await client.post(
        "/admin/providers",
        json={"name": "openai", "kind": "openai"},
        headers=auth(token),
    )
    assert p2.status_code == 201

    binding = await client.post(
        "/admin/capability-bindings",
        json={
            "capability": "chat",
            "provider_id": p1.json()["id"],
            "model": "local-model",
        },
        headers=auth(token),
    )
    assert binding.status_code == 201
    binding_id = binding.json()["id"]

    async with get_sessionmaker()() as session:
        b = await get_capability_binding(session, "chat")
        assert b is not None and b.model == "local-model"
        assert b.provider.name == "ollama"

    # change the binding via API; new reads see it without restart
    patched = await client.patch(
        f"/admin/capability-bindings/{binding_id}",
        json={"provider_id": p2.json()["id"], "model": "gpt-4o-mini"},
        headers=auth(token),
    )
    assert patched.status_code == 200

    async with get_sessionmaker()() as session:
        b = await get_capability_binding(session, "chat")
        assert b is not None
        assert b.model == "gpt-4o-mini"
        assert b.provider.name == "openai"


async def test_role_specific_binding_wins(engine, client: httpx.AsyncClient) -> None:
    await make_user("admin", "admin")
    token = await login(client, "admin")
    p = await client.post(
        "/admin/providers",
        json={"name": "p", "kind": "openai"},
        headers=auth(token),
    )
    pid = p.json()["id"]
    roles = await client.get("/admin/roles", headers=auth(token))
    child_role = next(r for r in roles.json() if r["name"] == "child")

    await client.post(
        "/admin/capability-bindings",
        json={"capability": "chat", "provider_id": pid, "model": "default-model"},
        headers=auth(token),
    )
    await client.post(
        "/admin/capability-bindings",
        json={
            "capability": "chat",
            "provider_id": pid,
            "model": "child-model",
            "role_id": child_role["id"],
        },
        headers=auth(token),
    )

    async with get_sessionmaker()() as session:
        b_child = await get_capability_binding(session, "chat", child_role["id"])
        assert b_child is not None and b_child.model == "child-model"
        b_other = await get_capability_binding(session, "chat", 999)
        assert b_other is not None and b_other.model == "default-model"


async def test_named_models_crud(engine, client: httpx.AsyncClient) -> None:
    await make_user("admin", "admin")
    token = await login(client, "admin")

    p = await client.post(
        "/admin/providers",
        json={
            "name": "yandex",
            "kind": "openai-compatible",
            "base_url": "https://ai.api.cloud.yandex.net/v1/chat/completions",
            "api_key_ref": "yandex_api_key",
        },
        headers=auth(token),
    )
    assert p.status_code == 201
    # stored verbatim; the /chat/completions suffix is normalized at use time
    assert p.json()["base_url"] == (
        "https://ai.api.cloud.yandex.net/v1/chat/completions"
    )
    pid = p.json()["id"]

    patched_p = await client.patch(
        f"/admin/providers/{pid}",
        json={"base_url": "https://ai.api.cloud.yandex.net/v1"},
        headers=auth(token),
    )
    assert patched_p.status_code == 200
    assert patched_p.json()["base_url"] == "https://ai.api.cloud.yandex.net/v1"

    created = await client.post(
        "/admin/named-models",
        json={
            "name": "Алиса",
            "provider_id": pid,
            "model": "aliceai-llm/latest",
            "system_prompt": "Отвечай как Алиса.",
        },
        headers=auth(token),
    )
    assert created.status_code == 201, created.text
    mid = created.json()["id"]

    listed = await client.get("/admin/named-models", headers=auth(token))
    assert [m["name"] for m in listed.json()] == ["Алиса"]

    patched = await client.patch(
        f"/admin/named-models/{mid}",
        json={"enabled": False},
        headers=auth(token),
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False

    bad = await client.post(
        "/admin/named-models",
        json={"name": "Лиля", "provider_id": 9999, "model": "m"},
        headers=auth(token),
    )
    assert bad.status_code == 404

    assert (
        await client.delete(f"/admin/named-models/{mid}", headers=auth(token))
    ).status_code == 200
    # deleting a provider removes its named models
    await client.post(
        "/admin/named-models",
        json={"name": "Лилия", "provider_id": pid, "model": "m"},
        headers=auth(token),
    )
    assert (
        await client.delete(f"/admin/providers/{pid}", headers=auth(token))
    ).status_code == 200
    listed = await client.get("/admin/named-models", headers=auth(token))
    assert listed.json() == []


async def test_mcp_and_code_agent_crud(engine, client: httpx.AsyncClient) -> None:
    await make_user("admin", "admin")
    token = await login(client, "admin")

    mcp = await client.post(
        "/admin/mcp-servers",
        json={
            "name": "notes",
            "transport": "stdio",
            "command": "mcp-notes",
            "allowed_tools": ["search"],
        },
        headers=auth(token),
    )
    assert mcp.status_code == 201
    mcp_id = mcp.json()["id"]
    patched = await client.patch(
        f"/admin/mcp-servers/{mcp_id}",
        json={"enabled": False},
        headers=auth(token),
    )
    assert patched.json()["enabled"] is False
    assert (
        await client.delete(f"/admin/mcp-servers/{mcp_id}", headers=auth(token))
    ).status_code == 200

    agent = await client.post(
        "/admin/code-agents",
        json={
            "name": "devin",
            "ssh_host": "mac",
            "workdir": "~/projects",
            "is_default": True,
        },
        headers=auth(token),
    )
    assert agent.status_code == 201
    assert (
        await client.delete(
            f"/admin/code-agents/{agent.json()['id']}", headers=auth(token)
        )
    ).status_code == 200
