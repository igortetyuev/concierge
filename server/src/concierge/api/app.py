from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from concierge.api.health import router as health_router
from concierge.core.db import dispose_engine


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="concierge", lifespan=lifespan)
    app.include_router(health_router)
    return app


app = create_app()
