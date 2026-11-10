import asyncio

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from concierge.core.config import get_settings
from concierge.core.db import get_engine

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
