import httpx

from concierge.auth.models import User
from concierge.conversations.models import Conversation, Run
from concierge.core import ratelimit
from concierge.core.db import get_sessionmaker
from tests.conftest import auth, login, make_user


async def test_metrics(engine, client: httpx.AsyncClient) -> None:
    await make_user("admin", "admin")
    token = await login(client, "admin")
    resp = await client.get("/metrics", headers=auth(token))
    assert resp.status_code == 200
    assert "concierge_users_total" in resp.text
    assert "concierge_telegram_running" in resp.text


async def test_metrics_requires_admin(engine, client: httpx.AsyncClient) -> None:
    await make_user("kid", "child")
    token = await login(client, "kid")
    assert (await client.get("/metrics", headers=auth(token))).status_code == 403


async def test_audit_log(engine, client: httpx.AsyncClient) -> None:
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
    assert created.status_code == 201

    resp = await client.get("/admin/audit-log", headers=auth(token))
    assert resp.status_code == 200
    events = resp.json()["events"]
    actions = [e["action"] for e in events]
    assert "auth.login" in actions
    assert "admin.user.create" in actions
    create_event = next(e for e in events if e["action"] == "admin.user.create")
    assert create_event["actor_name"] == "admin"
    assert create_event["target"] == "mom"

    # settings writes are audited too
    await client.put(
        "/settings/global",
        json={"key": "agent.name", "value": "Консьерж"},
        headers=auth(token),
    )
    resp = await client.get("/admin/audit-log", headers=auth(token))
    assert "settings.put" in [e["action"] for e in resp.json()["events"]]


async def test_runs_journal(engine, client: httpx.AsyncClient) -> None:
    admin = await make_user("admin", "admin")
    token = await login(client, "admin")
    async with get_sessionmaker()() as session:
        conv = Conversation(kind="personal", title="журнал", owner_id=admin.id)
        session.add(conv)
        await session.flush()
        session.add(
            Run(
                conversation_id=conv.id,
                status="done",
                model="openai/gpt-x",
                usage={"input_tokens": 10, "output_tokens": 20},
            )
        )
        await session.commit()

    resp = await client.get("/admin/runs", headers=auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    run = body["runs"][0]
    assert run["model"] == "openai/gpt-x"
    assert run["conversation_title"] == "журнал"
    assert run["usage"]["output_tokens"] == 20


async def test_prompt_preview(engine, client: httpx.AsyncClient) -> None:
    admin = await make_user("admin", "admin")
    async with get_sessionmaker()() as session:
        user = await session.get(User, admin.id)
        assert user is not None
        user.prompt_fragment = "любит чай"
        user.role.prompt_fragment = "ролевой фрагмент"
        await session.commit()
    token = await login(client, "admin")
    resp = await client.get(
        f"/admin/prompt-preview?user_id={admin.id}", headers=auth(token)
    )
    assert resp.status_code == 200
    text = resp.json()["prompt"]
    assert "консьерж" in text.lower()
    assert "[Роль: admin]" in text
    assert "любит чай" in text

    missing = await client.get(
        "/admin/prompt-preview?user_id=9999", headers=auth(token)
    )
    assert missing.status_code == 404


async def test_login_rate_limit(engine, client: httpx.AsyncClient) -> None:
    await make_user("admin", "admin")
    for _ in range(ratelimit.LOGIN_FAIL_LIMIT):
        resp = await client.post(
            "/auth/login", json={"username": "admin", "password": "bad"}
        )
        assert resp.status_code == 401
    resp = await client.post(
        "/auth/login", json={"username": "admin", "password": "bad"}
    )
    assert resp.status_code == 429
    # even the correct password is rejected while the window is saturated
    resp = await client.post(
        "/auth/login", json={"username": "admin", "password": "password123"}
    )
    assert resp.status_code == 429


async def test_link_code_rate_limit(engine, client: httpx.AsyncClient) -> None:
    await make_user("u", "adult")
    token = await login(client, "u")
    for _ in range(ratelimit.LINK_CODE_LIMIT):
        resp = await client.post("/telegram/link-code", headers=auth(token))
        assert resp.status_code == 200
    resp = await client.post("/telegram/link-code", headers=auth(token))
    assert resp.status_code == 429


async def test_body_size_limit(engine, client: httpx.AsyncClient) -> None:
    await make_user("admin", "admin")
    token = await login(client, "admin")
    resp = await client.post(
        "/conversations",
        json={"kind": "personal", "title": "x" * 1_100_000, "member_ids": []},
        headers=auth(token),
    )
    assert resp.status_code == 413
