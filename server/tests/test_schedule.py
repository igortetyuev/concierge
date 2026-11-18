"""Scheduler: recurrence math, job firing, session rules, tools, REST."""

from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from concierge.agent.models import CapabilityBinding, ModelProvider
from concierge.agent.runtime import get_runtime
from concierge.auth.models import User
from concierge.conversations import service as conversations
from concierge.conversations.models import Conversation, Message
from concierge.core import db
from concierge.schedule import recurrence, service
from concierge.schedule.models import ScheduledJob
from concierge.schedule.recurrence import ScheduleSpec, family_tz

from .conftest import auth, login, make_user

_provider_seq = 0


async def _add_chat_provider() -> None:
    global _provider_seq
    _provider_seq += 1
    async with db.get_sessionmaker()() as session:
        provider = ModelProvider(
            name=f"p-sched-{_provider_seq}", kind="test"
        )
        session.add(provider)
        await session.flush()
        session.add(
            CapabilityBinding(
                capability="chat",
                provider_id=provider.id,
                model="test-model",
                params={
                    "custom_output_text": "ответ планировщика",
                    "call_tools": ["current_datetime"],
                },
            )
        )
        await session.commit()


TZ = family_tz()
NOW = datetime(2026, 11, 18, 10, 0)  # Wednesday, naive local


def _next(spec: ScheduleSpec, after_local: datetime) -> datetime | None:
    after_utc = recurrence.local_to_utc(after_local, TZ)
    out = recurrence.next_after(spec, TZ, after_utc)
    return recurrence.utc_to_local(out, TZ) if out else None


# ---------- recurrence ----------


def test_spec_once_and_delay() -> None:
    assert _next(ScheduleSpec(at=NOW + timedelta(hours=2)), NOW) == NOW + timedelta(hours=2)
    assert _next(ScheduleSpec(at=NOW - timedelta(hours=2)), NOW) is None
    n = recurrence.normalize_oneshot(
        ScheduleSpec(delay_minutes=30), NOW
    )
    assert n.at == NOW + timedelta(minutes=30)
    with pytest.raises(ValueError):
        ScheduleSpec(at=NOW, delay_minutes=5)
    with pytest.raises(ValueError):
        ScheduleSpec(at=NOW, freq="daily")
    with pytest.raises(ValueError):
        ScheduleSpec()


def test_spec_minutely_hourly_anchored() -> None:
    spec = ScheduleSpec(freq="minutely", interval=15, start=NOW)
    assert _next(spec, NOW + timedelta(minutes=1)) == NOW + timedelta(minutes=15)
    # Anchor must not drift: after a fire, the grid stays on :00/:15/:30/:45.
    assert _next(spec, NOW + timedelta(minutes=16)) == NOW + timedelta(
        minutes=30
    )
    spec = ScheduleSpec(freq="hourly", interval=2, start=NOW)
    assert _next(spec, NOW + timedelta(minutes=30)) == NOW + timedelta(hours=2)


def test_spec_weekly_weekdays_times() -> None:
    spec = ScheduleSpec(
        freq="weekly",
        weekdays=["mo", "fr"],
        times=["08:00", "18:30"],
        start=NOW,
    )
    n = _next(spec, NOW)
    # Wednesday isn't in the set; next is Friday 08:00.
    assert n == datetime(2026, 11, 20, 8, 0)


def test_weekly_next_is_friday() -> None:
    spec = ScheduleSpec(
        freq="weekly", weekdays=["fr"], times=["08:00"], start=NOW
    )
    n = _next(spec, NOW)
    assert n is not None and n.weekday() == 4 and n.hour == 8


def test_spec_monthly_last_day_and_yearly() -> None:
    spec = ScheduleSpec(
        freq="monthly", month_days=[-1], times=["09:00"], start=NOW
    )
    n = _next(spec, NOW)
    assert n == datetime(2026, 11, 30, 9, 0)
    spec = ScheduleSpec(freq="yearly", start=datetime(2026, 3, 1, 9, 0))
    n = _next(spec, NOW)
    assert n == datetime(2027, 3, 1, 9, 0)


def test_spec_dates_only_exclude_and_bounds() -> None:
    d1, d2 = NOW + timedelta(days=1), NOW + timedelta(days=2)
    spec = ScheduleSpec(dates=[d1, d2])
    assert _next(spec, NOW) == d1
    spec = ScheduleSpec(dates=[d1, d2], exclude_dates=[d1.date()])
    assert _next(spec, NOW) == d2
    # recurrence with excluded date skips that whole day
    spec = ScheduleSpec(
        freq="daily", times=["08:00"], start=NOW,
        exclude_dates=[(NOW + timedelta(days=1)).date()],
    )
    n = _next(spec, NOW + timedelta(days=1, hours=1))
    assert n == NOW + timedelta(days=2, hours=-2)
    # until / count exhaustion
    spec = ScheduleSpec(
        freq="daily", start=NOW, until=NOW + timedelta(days=1, hours=1)
    )
    assert _next(spec, NOW + timedelta(days=2)) is None
    spec = ScheduleSpec(freq="daily", count=1, start=NOW)
    assert _next(spec, NOW + timedelta(days=1, hours=1)) is None


def test_pause_skips_occurrences() -> None:
    spec = ScheduleSpec(freq="daily", times=["08:00"], start=NOW)
    after_utc = recurrence.local_to_utc(NOW, TZ)
    pause_utc = recurrence.local_to_utc(NOW + timedelta(days=3), TZ)
    out = recurrence.next_after(spec, TZ, after_utc, paused_until=pause_utc)
    assert out is not None
    assert recurrence.utc_to_local(out, TZ) == datetime(2026, 11, 22, 8, 0)


def test_daily_or_rarer_classification() -> None:
    assert recurrence.is_daily_or_rarer(ScheduleSpec(at=NOW), TZ, NOW)
    assert recurrence.is_daily_or_rarer(
        ScheduleSpec(freq="daily", times=["08:00"], start=NOW), TZ, NOW
    )
    assert not recurrence.is_daily_or_rarer(
        ScheduleSpec(freq="daily", times=["08:00", "20:00"], start=NOW),
        TZ,
        NOW,
    )
    assert not recurrence.is_daily_or_rarer(
        ScheduleSpec(freq="hourly", start=NOW), TZ, NOW
    )
    assert recurrence.is_daily_or_rarer(
        ScheduleSpec(freq="weekly", weekdays=["mo", "fr"], times=["08:00"], start=NOW),
        TZ,
        NOW,
    )


def test_describe() -> None:
    assert "однократно" in recurrence.describe(ScheduleSpec(at=NOW))
    assert "пн" in recurrence.describe(
        ScheduleSpec(freq="weekly", weekdays=["mo"], times=["08:00"], start=NOW)
    )
    assert "каждые 15 минут" in recurrence.describe(
        ScheduleSpec(freq="minutely", interval=15, start=NOW)
    )


# ---------- service + runtime ----------


async def _make_job(
    user: User,
    kind: str,
    spec: ScheduleSpec,
    conversation_id: int | None = None,
    now: datetime | None = None,
    title: str = "тест",
    text: str = "сделай что-то",
) -> ScheduledJob:
    async with db.get_sessionmaker()() as session:
        return await service.create_job(
            session, user, kind, title, text, spec, conversation_id,
            now or db.utcnow(),
        )


async def _conv(conv_id: int) -> Conversation:
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, conv_id)
        assert conv is not None
        return conv


async def _last_message(conv_id: int) -> Message:
    async with db.get_sessionmaker()() as session:
        return (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conv_id)
                .order_by(Message.seq.desc())
                .limit(1)
            )
        ).scalars().one()


async def test_command_daily_new_session_each_fire(engine) -> None:
    await _add_chat_provider()
    user = await make_user("sched_a", "adult")
    spec = ScheduleSpec(freq="daily", times=["08:00"], start=NOW)
    job = await _make_job(user, "command", spec)
    assert job.session_mode == "new"

    assert job.next_run_at is not None
    assert await service.fire_due(job.next_run_at + timedelta(minutes=1)) == 1
    nxt = job.next_run_at
    async with db.get_sessionmaker()() as session:
        nxt = (await session.get(ScheduledJob, job.id)).next_run_at
    assert nxt is not None
    assert await service.fire_due(nxt + timedelta(minutes=1)) == 1
    await get_runtime().wait_idle()

    async with db.get_sessionmaker()() as session:
        convs = (
            await session.execute(
                select(Conversation).where(Conversation.origin == "schedule")
            )
        ).scalars().all()
    assert len(convs) == 2
    assert {c.title for c in convs} == {"тест"}
    assert all(
        c.settings.get("schedule_job_id") == job.id for c in convs
    )
    # The run actually executed: last message is the assistant's.
    reply = await _last_message(convs[-1].id)
    assert reply.author_kind == "assistant"


async def test_command_hourly_reuses_session(engine) -> None:
    await _add_chat_provider()
    user = await make_user("sched_b", "adult")
    spec = ScheduleSpec(freq="hourly", start=NOW)
    job = await _make_job(user, "command", spec)
    assert job.session_mode == "reuse"

    assert job.next_run_at is not None
    await service.fire_due(job.next_run_at + timedelta(minutes=1))
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        job = await session.get(ScheduledJob, job.id)
        conv_id = job.conversation_id
        assert conv_id is not None
        t2 = job.next_run_at

    assert t2 is not None
    await service.fire_due(t2 + timedelta(minutes=1))
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        job = await session.get(ScheduledJob, job.id)
        assert job.conversation_id == conv_id  # same session

        # archive -> next fire creates a fresh session
        conv = await session.get(Conversation, conv_id)
        await conversations.update_conversation(
            session, conv, status="archived"  # type: ignore[arg-type]
        )
        t3 = (await session.get(ScheduledJob, job.id)).next_run_at
    assert t3 is not None
    await service.fire_due(t3 + timedelta(minutes=1))
    async with db.get_sessionmaker()() as session:
        job = await session.get(ScheduledJob, job.id)
        assert job.conversation_id != conv_id


async def test_reminder_fires_in_origin_session(engine, client) -> None:
    await _add_chat_provider()
    user = await make_user("sched_c", "adult")
    token = await login(client, "sched_c")
    resp = await client.post(
        "/conversations", json={"kind": "personal", "member_ids": []},
        headers=auth(token),
    )
    conv_id = resp.json()["id"]

    at_local = recurrence.utc_to_local(db.utcnow(), TZ) + timedelta(minutes=5)
    spec = ScheduleSpec(at=at_local)
    job = await _make_job(
        user, "reminder", spec, conversation_id=conv_id,
        title="плита", text="выключить плиту",
    )
    assert job.next_run_at is not None
    assert await service.fire_due(job.next_run_at + timedelta(minutes=1)) == 1
    await get_runtime().wait_idle()

    reply = await _last_message(conv_id)
    assert reply.author_kind == "assistant"
    # one-shot jobs are deleted after firing
    async with db.get_sessionmaker()() as session:
        assert await session.get(ScheduledJob, job.id) is None


async def test_reminder_inside_scheduler_session(engine) -> None:
    """A reminder asked inside a scheduler-created session fires there."""
    await _add_chat_provider()
    user = await make_user("sched_r", "adult")
    job_cmd = await _make_job(
        user, "command", ScheduleSpec(freq="daily", times=["08:00"], start=NOW)
    )
    assert job_cmd.next_run_at is not None
    await service.fire_due(job_cmd.next_run_at + timedelta(minutes=1))
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        conv_id = (
            await session.get(ScheduledJob, job_cmd.id)
        ).conversation_id
    assert conv_id is not None
    # "Напомни через 5 минут" inside that session
    at_local = recurrence.utc_to_local(db.utcnow(), TZ) + timedelta(minutes=5)
    job_rem = await _make_job(
        user, "reminder", ScheduleSpec(at=at_local), conversation_id=conv_id
    )
    assert job_rem.next_run_at is not None
    await service.fire_due(job_rem.next_run_at + timedelta(minutes=1))
    await get_runtime().wait_idle()
    reply = await _last_message(conv_id)
    assert reply.author_kind == "assistant"


async def test_reminder_archived_restored_deleted_replaced(engine, client) -> None:
    await _add_chat_provider()
    user = await make_user("sched_d", "adult")
    token = await login(client, "sched_d")
    resp = await client.post(
        "/conversations", json={"kind": "personal", "member_ids": []},
        headers=auth(token),
    )
    conv_id = resp.json()["id"]

    spec = ScheduleSpec(freq="daily", times=["08:00"], start=NOW)
    job = await _make_job(
        user, "reminder", spec, conversation_id=conv_id
    )
    assert job.next_run_at is not None
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, conv_id)
        await conversations.update_conversation(
            session, conv, status="archived"  # type: ignore[arg-type]
        )
    await service.fire_due(job.next_run_at + timedelta(minutes=1))
    conv = await _conv(conv_id)
    assert conv.status == "active"  # restored

    # deleted -> a fresh personal session, job retargeted
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, conv_id)
        await conversations.update_conversation(
            session, conv, status="deleted"  # type: ignore[arg-type]
        )
        nxt = (await session.get(ScheduledJob, job.id)).next_run_at
    assert nxt is not None
    await service.fire_due(nxt + timedelta(minutes=1))
    async with db.get_sessionmaker()() as session:
        job = await session.get(ScheduledJob, job.id)
        assert job.conversation_id not in (None, conv_id)
        new_conv = await session.get(Conversation, job.conversation_id)
        assert new_conv.status == "active"


async def test_fire_idempotent_same_occurrence(engine, client) -> None:
    await _add_chat_provider()
    user = await make_user("sched_e", "adult")
    token = await login(client, "sched_e")
    resp = await client.post(
        "/conversations", json={"kind": "personal", "member_ids": []},
        headers=auth(token),
    )
    conv_id = resp.json()["id"]
    spec = ScheduleSpec(freq="hourly", start=NOW)
    job = await _make_job(
        user, "reminder", spec, conversation_id=conv_id
    )
    occ = db.utcnow()
    await service.fire(job.id, occ, db.utcnow())
    await service.fire(job.id, occ, db.utcnow())
    async with db.get_sessionmaker()() as session:
        n = (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.conversation_id == conv_id,
                    Message.source_channel == "schedule",
                )
            )
        ).scalar_one()
    assert n == 1


async def test_missed_fire_coalesces_and_grace(engine, client) -> None:
    await _add_chat_provider()
    user = await make_user("sched_f", "adult")
    token = await login(client, "sched_f")
    resp = await client.post(
        "/conversations", json={"kind": "personal", "member_ids": []},
        headers=auth(token),
    )
    conv_id = resp.json()["id"]
    spec = ScheduleSpec(freq="minutely", interval=1, start=NOW)
    job = await _make_job(
        user, "reminder", spec, conversation_id=conv_id
    )
    # 3h later (within grace? no — 3h < 12h grace): exactly one late fire
    later = db.utcnow() + timedelta(hours=3)
    # force next_run_at into the past relative to `later`
    async with db.get_sessionmaker()() as session:
        j = await session.get(ScheduledJob, job.id)
        j.next_run_at = db.utcnow()
        await session.commit()
    fired = await service.fire_due(later)
    assert fired == 1
    # >12h late: skipped, no message, next_run_at in the future
    much_later = db.utcnow() + timedelta(days=2)
    async with db.get_sessionmaker()() as session:
        j = await session.get(ScheduledJob, job.id)
        j.next_run_at = db.utcnow() - timedelta(days=2)
        await session.commit()
    fired = await service.fire_due(much_later)
    assert fired == 0


async def test_pause_and_resume(engine, client) -> None:
    user = await make_user("sched_g", "adult")
    spec = ScheduleSpec(freq="daily", times=["08:00"], start=NOW)
    job = await _make_job(user, "command", spec)
    async with db.get_sessionmaker()() as session:
        j = await session.get(ScheduledJob, job.id)
        await service.pause_job(session, j, NOW + timedelta(days=5))
        assert j.paused_until is not None
        assert j.next_run_at > recurrence.local_to_utc(
            NOW + timedelta(days=4), TZ
        )
        await service.resume_job(session, j)
        assert j.paused_until is None
        assert j.next_run_at is not None
        await service.cancel_job(session, j)
        assert await session.get(ScheduledJob, job.id) is None


async def test_mention_mode_session_fires(engine, client) -> None:
    """A scheduled trigger wakes a reply_mode=mention group session."""
    await _add_chat_provider()
    user = await make_user("sched_h", "adult")
    other = await make_user("sched_i", "adult")
    async with db.get_sessionmaker()() as session:
        conv = await conversations.create_conversation(
            session, user, "group", "группа", [other.id]
        )
        assert conv is not None
        conv_id = conv.id
    at_local = recurrence.utc_to_local(db.utcnow(), TZ) + timedelta(minutes=1)
    spec = ScheduleSpec(at=at_local)
    job = await _make_job(
        user, "reminder", spec, conversation_id=conv_id
    )
    occ = db.utcnow()
    await service.fire(job.id, occ, db.utcnow())
    await get_runtime().wait_idle()
    reply = await _last_message(conv_id)
    assert reply.author_kind == "assistant"


async def test_consecutive_fires_queue(engine, client) -> None:
    """wait=True: two fires in the same session both produce replies."""
    await _add_chat_provider()
    user = await make_user("sched_j", "adult")
    token = await login(client, "sched_j")
    resp = await client.post(
        "/conversations", json={"kind": "personal", "member_ids": []},
        headers=auth(token),
    )
    conv_id = resp.json()["id"]
    spec = ScheduleSpec(freq="minutely", interval=1, start=NOW)
    job = await _make_job(
        user, "reminder", spec, conversation_id=conv_id
    )
    await service.fire(job.id, db.utcnow(), db.utcnow())
    await service.fire(
        job.id, db.utcnow() + timedelta(minutes=1), db.utcnow()
    )
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        n = (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.conversation_id == conv_id,
                    Message.author_kind == "assistant",
                )
            )
        ).scalar_one()
    assert n == 2


# ---------- tools + REST ----------


async def test_tools_gating() -> None:
    from concierge.agent.tools import CORE_TOOLS, allowed_tool_names

    for name in (
        "schedule_task",
        "list_scheduled_tasks",
        "pause_scheduled_task",
        "resume_scheduled_task",
        "cancel_scheduled_task",
    ):
        assert name in CORE_TOOLS
    guest = {"tools": {"allow": [], "deny": ["*"]}}
    assert "schedule_task" not in allowed_tool_names(guest)
    adult = {"tools": {"allow": ["*"], "deny": []}}
    assert "schedule_task" in allowed_tool_names(adult)


async def test_schedule_task_tool(engine) -> None:
    from unittest.mock import MagicMock

    from concierge.schedule import tools as schedule_tools

    user = await make_user("sched_k", "adult")
    ctx = MagicMock()
    ctx.deps.user_id = user.id
    ctx.deps.conversation_id = 999  # unused for command
    out = await schedule_tools.schedule_task(
        ctx,
        kind="command",
        title="сводка",
        text="пришли сводку",
        schedule=ScheduleSpec(freq="daily", times=["08:00"]),
    )
    assert out["ok"] and out["id"]
    assert out["session_mode"] == "new"
    assert out["next_runs"]
    listed = await schedule_tools.list_scheduled_tasks(ctx)
    assert [j["id"] for j in listed] == [out["id"]]
    assert (
        await schedule_tools.pause_scheduled_task(
            ctx, out["id"], NOW + timedelta(days=1)
        )
    )["ok"]
    assert (await schedule_tools.resume_scheduled_task(ctx, out["id"]))["ok"]
    assert (await schedule_tools.cancel_scheduled_task(ctx, out["id"]))["ok"]
    assert await schedule_tools.list_scheduled_tasks(ctx) == []
    # foreign job invisible
    assert "error" in await schedule_tools.cancel_scheduled_task(ctx, out["id"])


async def test_rest_schedules(engine, client: httpx.AsyncClient) -> None:
    user = await make_user("sched_l", "adult")
    await make_user("sched_m", "adult")
    token = await login(client, "sched_l")
    spec = ScheduleSpec(freq="daily", times=["08:00"], start=NOW)
    job = await _make_job(user, "command", spec, title="утро")

    resp = await client.get("/schedules", headers=auth(token))
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["id"] for r in rows] == [job.id]
    assert rows[0]["title"] == "утро"
    assert rows[0]["next_run_at"]

    pause_to = (datetime.now() + timedelta(days=3)).isoformat()
    resp = await client.patch(
        f"/schedules/{job.id}",
        json={"paused_until": pause_to},
        headers=auth(token),
    )
    assert resp.status_code == 200 and resp.json()["paused_until"]
    resp = await client.patch(
        f"/schedules/{job.id}", json={"paused_until": None}, headers=auth(token)
    )
    assert resp.status_code == 200 and resp.json()["paused_until"] is None

    resp = await client.delete(f"/schedules/{job.id}", headers=auth(token))
    assert resp.status_code == 200
    other_token = await login(client, "sched_m")
    resp = await client.delete(
        f"/schedules/{job.id}", headers=auth(other_token)
    )
    assert resp.status_code == 404
