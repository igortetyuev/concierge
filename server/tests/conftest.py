import os
import tempfile
from collections.abc import AsyncIterator

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("CONCIERGE_SECRET_KEY", Fernet.generate_key().decode())
os.environ.setdefault(
    "CONCIERGE_JWT_SECRET", "test-jwt-secret-0123456789abcdef0123456789"
)
os.environ.setdefault(
    "CONCIERGE_BLOB_DIR",
    tempfile.mkdtemp(prefix="concierge-test-blobs-"),
)

from concierge.api.app import create_app
from concierge.auth.models import Role, User
from concierge.auth.security import hash_password
from concierge.core import db, models, ratelimit  # noqa: F401  (registers all tables)
from concierge.core.config import get_settings
from concierge.core.db import Base
from concierge.settings import service as settings_service

# Mirrors the seeds in alembic/versions/0002_users_roles_settings.py.
ROLE_SEEDS = {
    "admin": {"*": True, "max_upload_mb": 200},
    "adult": {
        "settings": {"user": "rw", "global": "r"},
        "manage_users": False,
        "tools": {"allow": ["*"], "deny": []},
        "mcp_servers": ["*"],
        "capabilities_override": {},
        "max_upload_mb": 100,
        "code_agent": True,
        "telegram": True,
        "voice_quick_session": True,
        "family_memory": True,
    },
    "child": {
        "settings": {"user": "rw", "global": "r"},
        "manage_users": False,
        "tools": {"allow": ["*"], "deny": []},
        "mcp_servers": [],
        "capabilities_override": {},
        "max_upload_mb": 50,
        "code_agent": False,
        "telegram": True,
        "voice_quick_session": True,
    },
    "guest": {
        "settings": {"user": "r"},
        "manage_users": False,
        "tools": {"allow": [], "deny": ["*"]},
        "mcp_servers": [],
        "capabilities_override": {},
        "max_upload_mb": 10,
        "code_agent": False,
        "telegram": False,
        "voice_quick_session": False,
    },
    # Shared-device account (esp32 «Колонка»): its uploads go through
    # speaker-ID — the account alone doesn't identify the speaker.
    "speaker": {
        "settings": {"user": "rw", "global": "rw"},
        "manage_users": False,
        "tools": {"allow": ["*"], "deny": []},
        "mcp_servers": ["*"],
        "capabilities_override": {},
        "max_upload_mb": 100,
        "code_agent": True,
        "telegram": True,
        "voice_quick_session": True,
        "family_memory": True,
        "speaker_id": True,
    },
}


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    get_settings.cache_clear()
    ratelimit.get_limiter().clear()
    eng = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        json_serializer=db.json_serializer,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db.set_engine(eng)
    async with db.get_sessionmaker()() as session:
        for name, perms in ROLE_SEEDS.items():
            session.add(Role(name=name, permissions=perms))
        await session.commit()
    yield eng
    db.set_engine(None)
    await eng.dispose()
    settings_service.clear_cache()


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as c:
        yield c


async def make_user(
    username: str, role_name: str, password: str = "password123"
) -> User:
    async with db.get_sessionmaker()() as session:
        from sqlalchemy import select

        role = (
            await session.execute(select(Role).where(Role.name == role_name))
        ).scalar_one()
        user = User(
            username=username,
            display_name=username.title(),
            role_id=role.id,
            password_hash=hash_password(password),
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def login(client: httpx.AsyncClient, username: str) -> str:
    resp = await client.post(
        "/auth/login", json={"username": username, "password": "password123"}
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
