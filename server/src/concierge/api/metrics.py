"""Prometheus-text /metrics endpoint (admin-gated)."""

import time
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import ColumnElement, func, select

from concierge.auth.dependencies import SessionDep, require
from concierge.auth.models import User
from concierge.channels.telegram import gateway as tg_gateway
from concierge.conversations.models import Conversation, Message, Run
from concierge.media.models import Attachment
from concierge.memory.models import MemoryItem

router = APIRouter()

_started_at = time.monotonic()

ManageUsers = Annotated[User, Depends(require("manage_users"))]


def _line(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    if labels:
        inner = ",".join(f'{k}="{v}"' for k, v in labels.items())
        return f"{name}{{{inner}}} {value}"
    return f"{name} {value}"


@router.get("/metrics")
async def metrics(_: ManageUsers, session: SessionDep) -> PlainTextResponse:
    async def count(model: type, *where: ColumnElement[bool]) -> int:
        q = select(func.count()).select_from(model)
        if where:
            q = q.where(*where)
        return int((await session.execute(q)).scalar_one())

    lines = [
        _line("concierge_uptime_seconds", int(time.monotonic() - _started_at)),
        _line("concierge_users_total", await count(User)),
        _line(
            "concierge_conversations_total",
            await count(Conversation, Conversation.status == "active"),
            {"status": "active"},
        ),
        _line(
            "concierge_conversations_total",
            await count(Conversation, Conversation.status == "archived"),
            {"status": "archived"},
        ),
        _line("concierge_messages_total", await count(Message)),
        _line("concierge_attachments_total", await count(Attachment)),
        _line(
            "concierge_blob_bytes_total",
            int(
                (
                    await session.execute(
                        select(func.coalesce(func.sum(Attachment.size), 0))
                    )
                ).scalar_one()
            ),
        ),
        _line("concierge_memory_items_total", await count(MemoryItem)),
    ]
    for status in ("queued", "running", "done", "error"):
        lines.append(
            _line(
                "concierge_runs_total",
                await count(Run, Run.status == status),
                {"status": status},
            )
        )
    lines.append(
        _line("concierge_telegram_running", 1 if tg_gateway.is_running() else 0)
    )
    return PlainTextResponse("\n".join(lines) + "\n")
