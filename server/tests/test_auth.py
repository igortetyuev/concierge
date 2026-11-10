import httpx
from sqlalchemy import select

from concierge.api.app import create_app
from concierge.core.db import get_sessionmaker
from tests.conftest import auth, login, make_user


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    )


async def test_login_refresh_logout(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("admin", "admin")

    token = await login(client, "admin")
    me = await client.get("/auth/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["username"] == "admin"
    assert me.json()["role"] == "admin"

    refreshed = await client.post("/auth/refresh")
    assert refreshed.status_code == 200
    new_token = refreshed.json()["access_token"]
    me2 = await client.get("/auth/me", headers=auth(new_token))
    assert me2.status_code == 200

    # logout revokes the rotated refresh token
    assert (await client.post("/auth/logout")).status_code == 200
    assert (await client.post("/auth/refresh")).status_code == 401


async def test_login_wrong_password(engine, client: httpx.AsyncClient) -> None:
    await make_user("admin", "admin")
    resp = await client.post(
        "/auth/login", json={"username": "admin", "password": "wrong-password"}
    )
    assert resp.status_code == 401


async def test_revoked_device_cannot_refresh(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("admin", "admin")
    token = await login(client, "admin")

    devices = await client.get("/auth/devices", headers=auth(token))
    assert devices.status_code == 200
    assert len(devices.json()) == 1
    device_id = devices.json()[0]["id"]

    resp = await client.delete(f"/auth/devices/{device_id}", headers=auth(token))
    assert resp.status_code == 200
    assert (await client.post("/auth/refresh")).status_code == 401


async def test_invite_register_login(engine, client: httpx.AsyncClient) -> None:
    admin = await make_user("admin", "admin")
    token = await login(client, "admin")

    async with get_sessionmaker()() as session:
        from concierge.auth.models import Role

        child_role = (
            await session.execute(select(Role).where(Role.name == "child"))
        ).scalar_one()
        role_id = child_role.id

    invite = await client.post(
        "/invites", json={"role_id": role_id}, headers=auth(token)
    )
    assert invite.status_code == 200, invite.text
    invite_token = invite.json()["token"]

    async with _new_client() as fresh:
        reg = await fresh.post(
            f"/auth/register?token={invite_token}",
            json={
                "username": "kid",
                "display_name": "Kid",
                "password": "password123",
            },
        )
        assert reg.status_code == 200, reg.text
        assert reg.json()["user"]["role"] == "child"

        # invite is single-use
        again = await fresh.post(
            f"/auth/register?token={invite_token}",
            json={
                "username": "kid2",
                "display_name": "Kid2",
                "password": "password123",
            },
        )
        assert again.status_code == 400

        login_resp = await fresh.post(
            "/auth/login", json={"username": "kid", "password": "password123"}
        )
        assert login_resp.status_code == 200

    assert admin.id is not None


async def test_invites_require_admin(engine, client: httpx.AsyncClient) -> None:
    await make_user("kid", "child")
    token = await login(client, "kid")
    resp = await client.post("/invites", json={"role_id": 1}, headers=auth(token))
    assert resp.status_code == 403


async def test_register_bad_invite(engine, client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/auth/register?token=bogus",
        json={
            "username": "kid",
            "display_name": "Kid",
            "password": "password123",
        },
    )
    assert resp.status_code == 400
