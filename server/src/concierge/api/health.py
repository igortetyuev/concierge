import asyncio

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from concierge.auth.dependencies import CurrentUser, SessionDep
from concierge.core.config import get_settings
from concierge.core.db import get_engine
from concierge.settings import service as settings_service

router = APIRouter()


async def check_db() -> bool:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


async def check_qdrant() -> bool:
    try:
        async with httpx.AsyncClient(
            base_url=get_settings().qdrant_url, timeout=5.0
        ) as client:
            resp = await client.get("/healthz")
    except Exception:
        return False
    return resp.status_code == 200


@router.get("/healthz")
async def healthz() -> JSONResponse:
    db_ok, qdrant_ok = await asyncio.gather(check_db(), check_qdrant())
    content = {
        "db": "ok" if db_ok else "error",
        "qdrant": "ok" if qdrant_ok else "error",
    }
    return JSONResponse(content, status_code=200 if db_ok and qdrant_ok else 503)


@router.get("/speaker/status")
async def speaker_status(session: SessionDep, _user: CurrentUser) -> dict[str, bool]:
    """Living-room speaker (esp32home) reachability via its announced URL."""
    env = await settings_service.get_global(session, "mcp.env.livingroom")
    conf = env if isinstance(env, dict) else {}
    base = str(conf.get("SPEAKER_URL") or "").rstrip("/")
    if not base:
        return {"online": False}
    token = conf.get("SPEAKER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{base}/healthz", headers=headers)
        return {"online": resp.status_code == 200}
    except Exception:
        return {"online": False}
