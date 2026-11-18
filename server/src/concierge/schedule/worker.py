"""Background scheduler loop; mirrors the memory indexer pattern.

Single uvicorn worker process, so the loop is in-process like the hub.
"""

import asyncio
import contextlib
import logging
from datetime import datetime

from sqlalchemy import func, select

from concierge.core.db import get_sessionmaker, utcnow
from concierge.schedule.models import ScheduledJob
from concierge.schedule.service import fire_due

log = logging.getLogger(__name__)

MAX_SLEEP_SECONDS = 60.0

_worker: asyncio.Task[None] | None = None
_worker_loop: asyncio.AbstractEventLoop | None = None
_kick: asyncio.Event | None = None


def kick() -> None:
    """Nudge the loop after job changes; no-op when not running (tests)."""
    if _worker is not None and _worker_loop is not None and _kick is not None:
        _worker_loop.call_soon_threadsafe(_kick.set)


def start_worker() -> None:
    global _worker, _worker_loop
    if _worker is None:
        _worker_loop = asyncio.get_running_loop()
        _worker = asyncio.create_task(_loop())


async def stop_worker() -> None:
    global _worker, _worker_loop, _kick
    if _worker is not None:
        _worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _worker
        _worker = None
    _worker_loop = None
    _kick = None


async def _next_due_in() -> float:
    async with get_sessionmaker()() as session:
        nxt: datetime | None = (
            await session.execute(
                select(func.min(ScheduledJob.next_run_at)).where(
                    ScheduledJob.next_run_at.is_not(None)
                )
            )
        ).scalar_one()
    if nxt is None:
        return MAX_SLEEP_SECONDS
    return max(1.0, min(MAX_SLEEP_SECONDS, (nxt - utcnow()).total_seconds()))


async def _loop() -> None:
    global _kick
    _kick = asyncio.Event()
    while True:
        _kick.clear()
        try:
            n = await fire_due(utcnow())
            if n:
                log.info("scheduler fired %s job(s)", n)
        except Exception:
            log.exception("scheduler sweep failed")
        timeout = MAX_SLEEP_SECONDS
        try:
            timeout = await _next_due_in()
        except Exception:
            log.exception("scheduler next-due query failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_kick.wait(), timeout=timeout)
