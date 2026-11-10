from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from concierge.api.admin import router as admin_router
from concierge.api.auth import router as auth_router
from concierge.api.conversations import router as conversations_router
from concierge.api.health import router as health_router
from concierge.api.settings import router as settings_router
from concierge.api.users import router as users_router
from concierge.core.db import dispose_engine
from concierge.ws import router as ws_router


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="concierge", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(settings_router)
    app.include_router(admin_router)
    app.include_router(conversations_router)
    app.include_router(ws_router)
    return app


app = create_app()
