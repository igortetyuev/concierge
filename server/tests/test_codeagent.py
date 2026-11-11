import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from sqlalchemy import select

from concierge.agent import tools
from concierge.agent.models import CodeAgent
from concierge.agent.runtime import get_runtime
from concierge.channels import ChannelEvent, get_hub
from concierge.codeagent import backends
from concierge.codeagent import service as ca_service
from concierge.codeagent.backends import (
    CodeAgentEvent,
    DevinPrintBackend,
    PiBackend,
)
from concierge.codeagent.service import delegate
from concierge.conversations.models import Message, Run
from concierge.core import db
from concierge.settings import service as settings_service
from concierge.settings.models import SettingScope
from tests.conftest import auth, login, make_user
from tests.test_agent import _add_provider, _make_conv, _post_and_collect


class FakeRemote:
    """RemoteProcess stand-in fed scripted stdout lines."""

    def __init__(
        self,
        lines: list[str],
        exit_status: int | None = 0,
        stderr: str = "",
        delay: float = 0.0,
    ) -> None:
        self._lines = lines
        self._exit_status = exit_status
        self._stderr = stderr
        self._delay = delay
        self.written: list[str] = []
        self.argv: list[str] = []
        self.workdir = ""
        self.stdin_closed = False
        self.killed = False

    async def lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield line

    async def write_line(self, line: str) -> None:
        self.written.append(line)

    async def stdin_eof(self) -> None:
        self.stdin_closed = True

    def stderr_tail(self) -> str:
        return self._stderr

    @property
    def exit_status(self) -> int | None:
        return self._exit_status

    async def kill(self) -> None:
        self.killed = True


def _factory(remote: FakeRemote) -> Any:
    async def open_fake(workdir: str, argv: list[str]) -> FakeRemote:
        remote.argv = argv
        remote.workdir = workdir
        return remote

    return open_fake


async def _collect(
    backend: Any, task: str = "do it", workdir: str = "/w"
) -> list[CodeAgentEvent]:
    await backend.start(task, workdir)
    return [e async for e in backend.stream_events()]


async def test_devin_backend_argv_and_result(engine) -> None:
    remote = FakeRemote(["final answer\n"])
    backend = DevinPrintBackend(_factory(remote))
    events = await _collect(backend, task="-fix the bug", workdir="/w/proj")
    assert remote.argv == [
        "devin",
        "-p",
        "--permission-mode",
        "dangerous",
        "--respect-workspace-trust",
        "false",
        "--",
        "-fix the bug",
    ]
    assert remote.workdir == "/w/proj"
    result = events[-1]
    assert result.kind == "result" and result.ok
    assert result.text == "final answer"


async def test_devin_backend_heartbeat(engine, monkeypatch) -> None:
    monkeypatch.setattr(backends, "_HEARTBEAT_S", 0.02)
    remote = FakeRemote(["done\n"], delay=0.07)
    backend = DevinPrintBackend(_factory(remote))
    events = await _collect(backend)
    heartbeats = [
        e for e in events if e.kind == "text" and "devin работает" in e.text
    ]
    assert heartbeats
    assert events[-1].kind == "result" and events[-1].text == "done"


async def test_devin_backend_error_exit(engine) -> None:
    remote = FakeRemote([], exit_status=1, stderr="boom")
    backend = DevinPrintBackend(_factory(remote))
    events = await _collect(backend)
    assert events[-1].kind == "result" and not events[-1].ok
    assert events[-1].text == "boom"


PI_SCRIPT = [
    json.dumps({"type": "session", "id": "s", "cwd": "/w"}),
    json.dumps(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "part\u2028one "},
        },
        ensure_ascii=False,
    ),
    json.dumps(
        {
            "type": "tool_execution_start",
            "toolCallId": "t1",
            "toolName": "bash",
            "args": {"command": "ls -la"},
        }
    ),
    json.dumps(
        {"type": "tool_execution_end", "toolCallId": "t1", "isError": False}
    ),
    json.dumps(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "two"},
        }
    ),
    json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "part\u2028one two"}],
                "stopReason": "stop",
            },
        },
        ensure_ascii=False,
    ),
    json.dumps({"type": "agent_end"}),
    json.dumps({"type": "agent_settled", "aborted": False}),
]
# PI_SCRIPT[1] carries a raw U+2028 inside a JSON string (\n-split only).


async def test_pi_backend_events(engine) -> None:
    remote = FakeRemote(PI_SCRIPT)
    backend = PiBackend(_factory(remote))
    events = await _collect(backend)
    assert remote.stdin_closed
    assert remote.written == ["do it"]

    tools_ = [e.text for e in events if e.kind == "tool"]
    assert tools_ == ["bash: ls -la"]
    result = events[-1]
    assert result.kind == "result" and result.ok
    assert result.text == "part\u2028one two"


async def test_pi_backend_error_exit(engine) -> None:
    remote = FakeRemote(
        PI_SCRIPT[:1], exit_status=1, stderr="No API key found for model"
    )
    backend = PiBackend(_factory(remote))
    events = await _collect(backend)
    result = events[-1]
    assert result.kind == "result" and not result.ok
    assert "No API key found" in result.text


# ---------- service ----------


class StubBackend:
    name = "stub"

    def __init__(self, events: list[CodeAgentEvent] | None = None) -> None:
        self.events = events or [CodeAgentEvent("result", "report", ok=True)]
        self.started: tuple[str, str] | None = None
        self.cancelled = False

    async def start(self, task: str, workdir: str) -> None:
        self.started = (task, workdir)

    async def stream_events(self) -> AsyncIterator[CodeAgentEvent]:
        for ev in self.events:
            yield ev

    async def cancel(self) -> None:
        self.cancelled = True


async def _setup_agent(
    workdir: str = "/w",
    name: str = "devin",
    args: dict | None = None,
    is_default: bool = True,
    projects: list[dict] | None = None,
) -> int:
    async with db.get_sessionmaker()() as session:
        agent = CodeAgent(
            name=name,
            ssh_host="mac",
            workdir=workdir,
            args=args or {},
            is_default=is_default,
        )
        session.add(agent)
        await session.commit()
        agent_id = agent.id
    await _set_projects(
        projects
        if projects is not None
        else [{"name": "proj", "path": "/w/proj"}]
    )
    return agent_id


async def _set_projects(projects: list[dict]) -> None:
    async with db.get_sessionmaker()() as session:
        await settings_service.set_value(
            session,
            SettingScope.global_,
            0,
            "code_agent.projects",
            projects,
            None,
        )


_user_seq = 0


async def _delegate(project: str = "proj", backend: Any = None, **kw: Any) -> dict:
    global _user_seq
    _user_seq += 1
    user = await make_user(f"svc_user{_user_seq}", "adult")
    async with db.get_sessionmaker()() as session:
        events: list[CodeAgentEvent] = []

        async def on_event(ev: CodeAgentEvent) -> None:
            events.append(ev)

        result = await delegate(
            session,
            user,
            "task",
            project,
            on_event,
            make_backend=(lambda a, t: backend) if backend else None,
            **kw,
        )
    result["_events"] = events
    return result


async def test_delegate_unknown_project(engine) -> None:
    await _setup_agent(projects=[{"name": "proj", "path": "/w/proj"}])
    result = await _delegate(project="nope", backend=StubBackend())
    assert "unknown project" in result["error"]
    assert result["available"] == ["proj"]


async def test_delegate_rejects_bad_paths(engine) -> None:
    await _setup_agent(
        projects=[
            {"name": "outside", "path": "/etc/x"},
            {"name": "dots", "path": "/w/proj/../x"},
            {"name": "rel", "path": "w/proj"},
        ]
    )
    for name in ("outside", "dots", "rel"):
        result = await _delegate(project=name, backend=StubBackend())
        assert "error" in result, name


async def test_delegate_timeout_cancels(engine) -> None:
    class HangBackend(StubBackend):
        async def stream_events(self) -> AsyncIterator[CodeAgentEvent]:
            await asyncio.sleep(30)
            yield CodeAgentEvent("result", "late")

    backend = HangBackend()
    await _setup_agent(args={"timeout_s": 0.05})
    result = await _delegate(backend=backend)
    assert backend.cancelled
    assert not result["ok"] and "timed out" in result["error"]


async def test_delegate_success(engine) -> None:
    backend = StubBackend(
        [
            CodeAgentEvent("text", "working\n"),
            CodeAgentEvent("result", "final report", ok=True),
        ]
    )
    await _setup_agent()
    result = await _delegate(backend=backend)
    assert result["ok"] and result["output"] == "final report"
    assert backend.started == ("task", "/w/proj")


async def test_default_agent_switch(engine, client: httpx.AsyncClient) -> None:
    await make_user("admin1", "admin")
    token = await login(client, "admin1")
    a1 = await client.post(
        "/admin/code-agents",
        json={
            "name": "devin",
            "ssh_host": "mac",
            "workdir": "/w",
            "is_default": True,
        },
        headers=auth(token),
    )
    a2 = await client.post(
        "/admin/code-agents",
        json={
            "name": "pi",
            "ssh_host": "mac",
            "workdir": "/w",
            "is_default": False,
        },
        headers=auth(token),
    )
    await client.patch(
        f"/admin/code-agents/{a2.json()['id']}",
        json={"is_default": True},
        headers=auth(token),
    )
    agents = (await client.get("/admin/code-agents", headers=auth(token))).json()
    by_id = {a["id"]: a for a in agents}
    assert not by_id[a1.json()["id"]]["is_default"]
    assert by_id[a2.json()["id"]]["is_default"]

    await _set_projects([{"name": "proj", "path": "/w/proj"}])
    backend = StubBackend()
    result = await _delegate(backend=backend)
    assert result["agent"] == "pi"


# ---------- permissions ----------


def test_tool_visibility_by_role() -> None:
    assert "delegate_coding_task" in tools.allowed_tool_names({"*": True})
    adult = {
        "tools": {"allow": ["*"], "deny": []},
        "code_agent": True,
    }
    assert "delegate_coding_task" in tools.allowed_tool_names(adult)
    child = {
        "tools": {"allow": ["*"], "deny": []},
        "code_agent": False,
    }
    assert "delegate_coding_task" not in tools.allowed_tool_names(child)


# ---------- e2e through the agent run ----------


async def test_delegate_tool_e2e(engine, client: httpx.AsyncClient, monkeypatch) -> None:
    backend = StubBackend(
        [
            CodeAgentEvent("text", "editing files"),
            CodeAgentEvent("tool", "bash: pytest"),
            CodeAgentEvent("result", "all done", ok=True),
        ]
    )
    monkeypatch.setattr(
        ca_service, "_make_backend", lambda a, t: backend
    )
    await _add_provider(
        "chat",
        params={
            "custom_output_text": "готово",
            "call_tools": ["delegate_coding_task"],
        },
    )
    await _setup_agent(projects=[{"name": "a", "path": "/w/a"}])
    await make_user("delegator", "adult")
    token = await login(client, "delegator")
    cid = await _make_conv(client, token)

    events: list[ChannelEvent] = []

    class RecSink:
        presence = False
        user_id = 0

        async def emit(self, event: ChannelEvent) -> None:
            events.append(event)

    conn = get_hub().connect(RecSink())
    await get_hub().subscribe(conn, cid)
    try:
        msg = await _post_and_collect(client, token, cid, "сделай")
        await get_runtime().wait_idle()
    finally:
        await get_hub().disconnect(conn)

    tool_events = [
        e
        for e in events
        if e.type == "message.tool" and e.data.get("name") == "delegate_coding_task"
    ]
    assert any("progress" in e.data for e in tool_events)
    assert any(e.data.get("status") == "done" for e in tool_events)
    assert backend.started is not None
    assert backend.started[1] == "/w/a"

    async with db.get_sessionmaker()() as session:
        run = (
            await session.execute(
                select(Run).where(Run.trigger_message_id == msg["id"])
            )
        ).scalar_one()
        reply = (
            await session.execute(select(Message).where(Message.run_id == run.id))
        ).scalar_one()
    tool_parts = [p for p in reply.parts if p["type"] == "tool_call"]
    assert any(p["name"] == "delegate_coding_task" for p in tool_parts)


# ---------- telegram sink ----------


async def test_telegram_sink_tool_progress(engine, monkeypatch) -> None:
    from concierge.channels.telegram import sink as tg_sink
    from concierge.channels.telegram.sink import TelegramSink
    from tests.test_telegram import FakeOutbox

    monkeypatch.setattr(tg_sink, "EDIT_THROTTLE", 0)

    outbox = FakeOutbox()
    sink = TelegramSink(outbox, 100, 0)
    for progress in ("step one", "step two"):
        await sink.emit(
            ChannelEvent(
                "message.tool",
                1,
                {
                    "run_id": 7,
                    "type": "tool",
                    "name": "delegate_coding_task",
                    "tool_call_id": "call-1",
                    "status": "running",
                    "progress": progress,
                },
            )
        )
    texts = outbox.all_texts(100)
    assert len(texts) == 2  # send + throttled edit, not two tool entries
    assert "step two" in texts[-1]
    assert all(t.count("🔧") == 1 for t in texts)
