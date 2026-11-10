import httpx
import pytest

from concierge.api import health
from concierge.api.app import create_app


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    )


async def test_healthz_ok(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "check_db", lambda: _true())
    monkeypatch.setattr(health, "check_qdrant", lambda: _true())
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"db": "ok", "qdrant": "ok"}


async def test_healthz_db_down(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "check_db", lambda: _false())
    monkeypatch.setattr(health, "check_qdrant", lambda: _true())
    resp = await client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["db"] == "error"


async def _true() -> bool:
    return True


async def _false() -> bool:
    return False
