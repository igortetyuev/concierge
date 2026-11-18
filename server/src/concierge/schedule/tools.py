"""Agent tools for scheduled events and reminders.

A job fires an agent run authored by the job's owner (their role/rights):
`command` — the concierge executes `text` as the owner in a session of the
job's own; `reminder` — the concierge is nudged to remind about `text` in
the session where the reminder was asked for.
"""

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic_ai import RunContext

from concierge.agent.tools import ToolDeps
from concierge.auth.models import User
from concierge.core.db import get_sessionmaker, utcnow
from concierge.schedule import recurrence, service
from concierge.schedule.models import ScheduledJob
from concierge.schedule.recurrence import ScheduleSpec, family_tz


def _job_out(job: ScheduledJob, tz: ZoneInfo) -> dict[str, Any]:
    spec = recurrence.ScheduleSpec.model_validate(job.spec)
    now_local = recurrence.utc_to_local(utcnow(), tz)
    return {
        "id": job.id,
        "kind": job.kind,
        "title": job.title,
        "text": job.text,
        "schedule": recurrence.describe(spec),
        "session_mode": job.session_mode,
        "next_runs": [
            o.isoformat()
            for o in recurrence.upcoming(spec, tz, now_local, n=3)
        ],
        "paused_until": (
            recurrence.utc_to_local(job.paused_until, tz).isoformat()
            if job.paused_until
            else None
        ),
    }


async def schedule_task(
    ctx: RunContext[ToolDeps],
    kind: Literal["command", "reminder"],
    title: str,
    text: str,
    schedule: ScheduleSpec,
) -> dict[str, Any]:
    """Create a scheduled event or a reminder.

    kind="command": `text` is a self-contained instruction the concierge will
    execute as you when it fires (e.g. "пришли сводку погоды"). A job firing
    once a day or rarer gets a fresh session each time; a more frequent one
    reuses its own session. kind="reminder": the concierge reminds about
    `text` in THIS session ("напомни через 5 минут / в 18:00 / по будням").
    `schedule`: at or delay_minutes for one-shot, or freq (minutely, hourly,
    daily, weekly, monthly, yearly) with optional interval, weekdays,
    times ("HH:MM"), month_days, until, count, dates and exclude_dates.
    Times and dates are in the family timezone. `title` names the pattern.
    """
    async with get_sessionmaker()() as session:
        user = await session.get(User, ctx.deps.user_id)
        if user is None:
            return {"error": "user not found"}
        try:
            job = await service.create_job(
                session,
                user,
                kind,
                title,
                text,
                schedule,
                conversation_id=(
                    ctx.deps.conversation_id if kind == "reminder" else None
                ),
                now=utcnow(),
            )
        except ValueError as e:
            return {"error": str(e)}
    return {"ok": True, **_job_out(job, family_tz())}


async def list_scheduled_tasks(ctx: RunContext[ToolDeps]) -> list[dict[str, Any]]:
    """List your active scheduled events and reminders with next fire times."""
    async with get_sessionmaker()() as session:
        jobs = await service.list_jobs(session, ctx.deps.user_id)
    tz = family_tz()
    return [_job_out(j, tz) for j in jobs]


async def pause_scheduled_task(
    ctx: RunContext[ToolDeps], task_id: int, until: datetime
) -> dict[str, Any]:
    """Suspend a scheduled task until `until` (local datetime); fires that
    would happen inside the pause are skipped."""
    async with get_sessionmaker()() as session:
        job = await service.get_own_job(session, ctx.deps.user_id, task_id)
        if job is None:
            return {"error": f"no such scheduled task {task_id}"}
        await service.pause_job(session, job, until)
    return {"ok": True, **_job_out(job, family_tz())}


async def resume_scheduled_task(
    ctx: RunContext[ToolDeps], task_id: int
) -> dict[str, Any]:
    """Resume a paused scheduled task."""
    async with get_sessionmaker()() as session:
        job = await service.get_own_job(session, ctx.deps.user_id, task_id)
        if job is None:
            return {"error": f"no such scheduled task {task_id}"}
        await service.resume_job(session, job)
    return {"ok": True, **_job_out(job, family_tz())}


async def cancel_scheduled_task(
    ctx: RunContext[ToolDeps], task_id: int
) -> dict[str, Any]:
    """Cancel a scheduled task entirely — the pattern is deleted."""
    async with get_sessionmaker()() as session:
        job = await service.get_own_job(session, ctx.deps.user_id, task_id)
        if job is None:
            return {"error": f"no such scheduled task {task_id}"}
        title = job.title
        await service.cancel_job(session, job)
    return {"ok": True, "cancelled": title}
