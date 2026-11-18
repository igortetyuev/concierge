"""Scheduled jobs: creation, pause/resume/cancel, firing.

Fire = post a user-authored trigger message (source_channel="schedule") into
the resolved conversation and schedule a regular agent run, so the command
executes with the owner's role and permissions.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.channels import ChannelEvent, get_hub
from concierge.conversations import service as conversations
from concierge.conversations.models import Conversation
from concierge.core.db import get_sessionmaker, utcnow
from concierge.memory import indexer as memory_indexer
from concierge.schedule import recurrence
from concierge.schedule.models import ScheduledJob
from concierge.schedule.recurrence import ScheduleSpec, family_tz

log = logging.getLogger(__name__)

MAX_JOBS_PER_USER = 100
MISSED_GRACE = timedelta(hours=12)
MAX_FIRE_ATTEMPTS = 3
FIRE_RETRY_DELAY = timedelta(minutes=1)

_KINDS = ("command", "reminder")


def _kick() -> None:
    from concierge.schedule import worker

    worker.kick()


def _spec_of(job: ScheduledJob) -> ScheduleSpec:
    return ScheduleSpec.model_validate(job.spec)


async def create_job(
    session: AsyncSession,
    user: User,
    kind: str,
    title: str,
    text: str,
    spec: ScheduleSpec,
    conversation_id: int | None,
    now: datetime,
) -> ScheduledJob:
    if kind not in _KINDS:
        raise ValueError(f"bad kind {kind!r}")
    tz = family_tz()
    now_local = recurrence.utc_to_local(now, tz)
    spec = recurrence.normalize_oneshot(spec, now_local)
    if spec.freq is not None and spec.start is None:
        spec = spec.model_copy(update={"start": now_local})
    next_run = recurrence.next_after(spec, tz, now)
    if next_run is None:
        raise ValueError("в этом расписании нет будущих срабатываний")
    count = (
        await session.execute(
            select(func.count())
            .select_from(ScheduledJob)
            .where(ScheduledJob.user_id == user.id)
        )
    ).scalar_one()
    if count >= MAX_JOBS_PER_USER:
        raise ValueError(f"лимит {MAX_JOBS_PER_USER} активных паттернов")
    session_mode = (
        "new"
        if kind == "command"
        and recurrence.is_daily_or_rarer(spec, tz, now_local)
        else "reuse"
    )
    job = ScheduledJob(
        user_id=user.id,
        kind=kind,
        title=title.strip()[:256],
        text=text.strip(),
        spec=spec.model_dump(mode="json"),
        session_mode=session_mode,
        conversation_id=conversation_id if kind == "reminder" else None,
        next_run_at=next_run,
    )
    session.add(job)
    await session.commit()
    _kick()
    return job


async def list_jobs(session: AsyncSession, user_id: int) -> list[ScheduledJob]:
    rows = (
        await session.execute(
            select(ScheduledJob)
            .where(ScheduledJob.user_id == user_id)
            .order_by(ScheduledJob.id)
        )
    ).scalars()
    return list(rows)


async def get_own_job(
    session: AsyncSession, user_id: int, job_id: int
) -> ScheduledJob | None:
    job = await session.get(ScheduledJob, job_id)
    if job is None or job.user_id != user_id:
        return None
    return job


async def pause_job(
    session: AsyncSession, job: ScheduledJob, until_local: datetime
) -> None:
    """Suspend fires until `until_local` (family-local wall clock)."""
    tz = family_tz()
    paused_utc = recurrence.local_to_utc(
        recurrence.spec_to_local(until_local, tz), tz
    )
    job.paused_until = paused_utc
    job.next_run_at = recurrence.next_after(
        _spec_of(job), tz, utcnow(), paused_utc
    )
    await session.commit()
    _kick()


async def resume_job(session: AsyncSession, job: ScheduledJob) -> None:
    job.paused_until = None
    job.next_run_at = recurrence.next_after(_spec_of(job), family_tz(), utcnow())
    await session.commit()
    _kick()


async def cancel_job(session: AsyncSession, job: ScheduledJob) -> None:
    """Cancel the pattern: the row is simply deleted from the scheduler."""
    await session.delete(job)
    await session.commit()
    _kick()


async def _new_session(
    session: AsyncSession, job: ScheduledJob, owner: User
) -> Conversation:
    conv = await conversations.create_conversation(
        session, owner, "personal", job.title, [], origin="schedule"
    )
    assert conv is not None
    conv.settings = {**conv.settings, "schedule_job_id": job.id}
    await session.commit()
    return conv


async def _is_member(
    session: AsyncSession, conv: Conversation, user_id: int
) -> bool:
    from concierge.conversations.models import ConversationMember

    member = await session.get(ConversationMember, (conv.id, user_id))
    return member is not None


async def resolve_target(
    session: AsyncSession, job: ScheduledJob, owner: User
) -> tuple[Conversation, bool]:
    """Conversation for the next fire; bool = notify members (new/restored)."""
    if job.kind == "reminder":
        if job.conversation_id is not None:
            conv = await session.get(Conversation, job.conversation_id)
            if conv is not None and await _is_member(session, conv, owner.id):
                if conv.status == "active":
                    return conv, False
                if conv.status == "archived":
                    await conversations.update_conversation(
                        session, conv, status="active"
                    )
                    return conv, True
        # deleted conversation, lost membership, or dangling id
        conv = await _new_session(session, job, owner)
        job.conversation_id = conv.id
        await session.commit()
        return conv, True
    # command
    if job.session_mode == "reuse" and job.conversation_id is not None:
        conv = await session.get(Conversation, job.conversation_id)
        if (
            conv is not None
            and conv.status == "active"
            and await _is_member(session, conv, owner.id)
        ):
            return conv, False
    conv = await _new_session(session, job, owner)
    job.conversation_id = conv.id
    await session.commit()
    return conv, True


def _trigger_part(
    job: ScheduledJob, occurrence: datetime
) -> dict[str, Any]:
    return {
        "type": "text",
        "text": job.text,
        "scheduled": {
            "job_id": job.id,
            "kind": job.kind,
            "title": job.title,
            "occurrence": occurrence.isoformat(),
        },
    }


async def fire(job_id: int, occurrence: datetime, now: datetime) -> None:
    """Post the trigger message and schedule the agent run."""
    # Late import: agent.tools -> schedule.tools -> this module is part of
    # the runtime's own import chain.
    from concierge.agent.runtime import _message_dict, get_runtime

    session_maker = get_sessionmaker()
    async with session_maker() as session:
        job = await session.get(ScheduledJob, job_id)
        if job is None:
            return
        owner = await session.get(User, job.user_id)
        if owner is None or not owner.is_active:
            return
        conv, notify_members = await resolve_target(session, job, owner)
        msg, created = await conversations.post_message(
            session,
            conv,
            owner,
            [_trigger_part(job, occurrence)],
            f"sched:{job.id}:{occurrence.isoformat()}",
            source_channel="schedule",
        )
        out = _message_dict(msg)
        conv_id = conv.id
        msg_id = msg.id
        member_ids = {m.user_id for m in conv.members}
    if not created:
        return
    hub = get_hub()
    if notify_members:
        await hub.publish_users(
            member_ids,
            ChannelEvent(
                "conversation.updated",
                conv_id,
                {"conversation": {"id": conv_id}},
            ),
        )
    await hub.publish(
        ChannelEvent("message.created", conv_id, {"message": out})
    )
    memory_indexer.kick()
    get_runtime().schedule(conv_id, msg_id, wait=True)


async def fire_due(now: datetime) -> int:
    """Claim and fire every due job. Returns the number actually fired.

    Each job's next_run_at is advanced (and committed) before firing, so a
    fire is at-most-once. Missed occurrences older than MISSED_GRACE are
    skipped; younger misses coalesce into a single late fire. A failed fire
    is retried a few times (a one-shot must not vanish unfired). Exhausted
    patterns (one-shot, until/count) are deleted after firing.
    """
    fired = 0
    tz = family_tz()
    session_maker = get_sessionmaker()
    while True:
        async with session_maker() as session:
            job = (
                await session.execute(
                    select(ScheduledJob)
                    .where(
                        ScheduledJob.next_run_at.is_not(None),
                        ScheduledJob.next_run_at <= now,
                    )
                    .order_by(ScheduledJob.next_run_at)
                    .limit(1)
                )
            ).scalars().first()
            if job is None:
                break
            job_id = job.id
            occurrence = job.next_run_at
            assert occurrence is not None
            spec = _spec_of(job)
            job.next_run_at = recurrence.next_after(spec, tz, now)
            job.last_run_at = now
            job.fire_count += 1
            exhausted = job.next_run_at is None
            await session.commit()  # claim
        fired_this = False
        if now - occurrence <= MISSED_GRACE:
            try:
                await fire(job_id, occurrence, now)
                fired_this = True
                fired += 1
            except Exception:
                log.exception("scheduled fire failed job=%s", job_id)
        else:
            log.warning(
                "scheduled fire skipped (too late: %s) job=%s",
                now - occurrence,
                job_id,
            )
        if not fired_this and now - occurrence <= MISSED_GRACE:
            # Retry instead of dropping the occurrence.
            async with session_maker() as session:
                job = await session.get(ScheduledJob, job_id)
                if job is not None and job.fire_count < MAX_FIRE_ATTEMPTS:
                    job.next_run_at = now + FIRE_RETRY_DELAY
                    await session.commit()
                    continue
        if exhausted:
            async with session_maker() as session:
                job = await session.get(ScheduledJob, job_id)
                if job is not None:
                    await session.delete(job)
                    await session.commit()
    return fired
