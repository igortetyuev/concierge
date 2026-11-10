import httpx

from concierge.core.db import get_sessionmaker
from concierge.settings import service
from concierge.settings.models import SettingScope
from tests.conftest import auth, login, make_user


async def test_layered_settings(engine, client: httpx.AsyncClient) -> None:
    admin = await make_user("admin", "admin")
    kid = await make_user("kid", "child")
    token = await login(client, "admin")

    async with get_sessionmaker()() as session:
        await service.set_value(
            session, SettingScope.global_, 0, "greeting", "global", admin.id
        )
        assert await service.get_value(session, kid, "greeting") == "global"

        await service.set_value(
            session, SettingScope.role, kid.role_id, "greeting", "role", admin.id
        )
        assert await service.get_value(session, kid, "greeting") == "role"

        await service.set_value(
            session, SettingScope.user, kid.id, "greeting", "user", kid.id
        )
        assert await service.get_value(session, kid, "greeting") == "user"

        assert await service.get_value(session, kid, "missing") is None
        assert await service.get_value(session, kid, "missing", "d") == "d"

    assert token


async def test_secret_settings_masked_and_encrypted(
    engine, client: httpx.AsyncClient
) -> None:
    admin = await make_user("admin", "admin")
    token = await login(client, "admin")

    put = await client.put(
        "/settings/global",
        json={"key": "providers.openai.api_key", "value": {"secret": "sk-live-123"}},
        headers=auth(token),
    )
    assert put.status_code == 200

    got = await client.get("/settings/global", headers=auth(token))
    assert got.json()["providers.openai.api_key"] == {"secret": "********"}

    async with get_sessionmaker()() as session:
        assert (
            await service.get_value(session, admin, "providers.openai.api_key")
            == "sk-live-123"
        )
        # stored value is ciphertext, not plaintext
        rows = await service.list_scope(session, SettingScope.global_, 0, reveal=True)
        stored = rows["providers.openai.api_key"]
        assert "sk-live-123" not in str(stored)

    # re-PUT the mask keeps the stored secret
    put2 = await client.put(
        "/settings/global",
        json={
            "key": "providers.openai.api_key",
            "value": {"secret": "********"},
        },
        headers=auth(token),
    )
    assert put2.status_code == 200
    async with get_sessionmaker()() as session:
        assert (
            await service.get_value(session, admin, "providers.openai.api_key")
            == "sk-live-123"
        )


async def test_delete_setting(engine, client: httpx.AsyncClient) -> None:
    await make_user("admin", "admin")
    token = await login(client, "admin")
    await client.put(
        "/settings/global",
        json={"key": "temp", "value": 1},
        headers=auth(token),
    )
    resp = await client.delete("/settings/global/temp", headers=auth(token))
    assert resp.status_code == 200
    got = await client.get("/settings/global", headers=auth(token))
    assert "temp" not in got.json()
