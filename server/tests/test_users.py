import httpx

from concierge.core import db
from tests.conftest import auth, login, make_user


async def test_users_lists_active_users(client: httpx.AsyncClient) -> None:
    await make_user("admin1", "admin")
    await make_user("boris", "adult")
    token = await login(client, "admin1")

    resp = await client.get("/users", headers=auth(token))
    assert resp.status_code == 200
    rows = resp.json()
    names = {r["username"] for r in rows}
    assert names == {"admin1", "boris"}
    assert rows[0].keys() == {"id", "username", "display_name"}


async def test_users_excludes_inactive(client: httpx.AsyncClient) -> None:
    inactive = await make_user("inactive1", "adult")
    async with db.get_sessionmaker()() as session:
        inactive.is_active = False
        session.add(inactive)
        await session.commit()
    await make_user("admin2", "admin")
    token = await login(client, "admin2")

    resp = await client.get("/users", headers=auth(token))
    assert resp.status_code == 200
    assert "inactive1" not in {r["username"] for r in resp.json()}


async def test_users_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/users")
    assert resp.status_code == 401
