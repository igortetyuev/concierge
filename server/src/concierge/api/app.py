from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from concierge.api.admin import router as admin_router
from concierge.api.attachments import router as attachments_router
from concierge.api.auth import router as auth_router
from concierge.api.conversations import router as conversations_router
from concierge.api.health import router as health_router
from concierge.api.memory import router as memory_router
from concierge.api.metrics import router as metrics_router
from concierge.api.settings import router as settings_router
from concierge.api.telegram import router as telegram_router
from concierge.api.users import router as users_router
from concierge.channels.telegram import gateway as telegram_gateway
from concierge.core.db import dispose_engine
from concierge.memory import indexer as memory_indexer
from concierge.ws import router as ws_router

# Non-multipart bodies (JSON API) are small; multipart uploads are capped by
# the per-role upload limit enforced in the attachments endpoint.
MAX_JSON_BODY = 1_000_000


class BodySizeLimitMiddleware:
    """Pure-ASGI content-length cap (BaseHTTPMiddleware would spawn a task)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] == "http":
            headers = {
                k.decode("latin-1"): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            length = headers.get("content-length")
            if (
                length is not None
                and not headers.get("content-type", "").startswith("multipart/")
                and int(length) > MAX_JSON_BODY
            ):
                response = JSONResponse(
                    {"detail": "request body too large"}, status_code=413
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    memory_indexer.start_worker()
    telegram_gateway.start_supervisor()
    yield
    await telegram_gateway.stop_supervisor()
    await memory_indexer.stop_worker()
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="concierge", lifespan=lifespan)
    app.add_middleware(BodySizeLimitMiddleware)

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(settings_router)
    app.include_router(admin_router)
    app.include_router(conversations_router)
    app.include_router(attachments_router)
    app.include_router(memory_router)
    app.include_router(telegram_router)
    app.include_router(metrics_router)
    app.include_router(ws_router)
    return app


app = create_app()
