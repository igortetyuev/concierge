from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from concierge.auth.dependencies import CurrentUser, SessionDep
from concierge.core.db import utcnow
from concierge.schedule import recurrence, service
from concierge.schedule.models import ScheduledJob
from concierge.schedule.recurrence import family_tz

router = APIRouter()


class ScheduleOut(BaseModel):
    id: int
    kind: str
    title: str
    text: str
    schedule: str
    session_mode: str
    conversation_id: int | None
    next_run_at: datetime | None
    next_runs: list[str]
    paused_until: datetime | None
    last_run_at: datetime | None
    fire_count: int
    created_at: datetime | None

    @classmethod
    def of(cls, job: ScheduledJob) -> "ScheduleOut":
        tz = family_tz()
        spec = recurrence.ScheduleSpec.model_validate(job.spec)
        return cls(
            id=job.id,
            kind=job.kind,
            title=job.title,
            text=job.text,
            schedule=recurrence.describe(spec),
            session_mode=job.session_mode,
            conversation_id=job.conversation_id,
            next_run_at=job.next_run_at,
            next_runs=[
                o.isoformat()
                for o in recurrence.upcoming(
                    spec, tz, recurrence.utc_to_local(utcnow(), tz), n=3
                )
            ],
            paused_until=job.paused_until,
            last_run_at=job.last_run_at,
            fire_count=job.fire_count,
            created_at=job.created_at,
        )


@router.get("/schedules")
async def list_schedules(
    user: CurrentUser, session: SessionDep
) -> list[ScheduleOut]:
    jobs = await service.list_jobs(session, user.id)
    return [ScheduleOut.of(j) for j in jobs]


class PatchScheduleBody(BaseModel):
    # Local datetime; null resumes the task.
    paused_until: datetime | None = None


@router.patch("/schedules/{job_id}")
async def patch_schedule(
    job_id: int,
    body: PatchScheduleBody,
    user: CurrentUser,
    session: SessionDep,
) -> ScheduleOut:
    job = await service.get_own_job(session, user.id, job_id)
    if job is None:
        raise HTTPException(404, "schedule not found")
    if body.paused_until is None:
        await service.resume_job(session, job)
    else:
        await service.pause_job(session, job, body.paused_until)
    return ScheduleOut.of(job)


@router.delete("/schedules/{job_id}")
async def delete_schedule(
    job_id: int, user: CurrentUser, session: SessionDep
) -> dict[str, bool]:
    job = await service.get_own_job(session, user.id, job_id)
    if job is None:
        raise HTTPException(404, "schedule not found")
    await service.cancel_job(session, job)
    return {"ok": True}
