"""Delegating coding tasks to a CLI code agent on the user's Mac."""

import asyncio
import posixpath
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.agent.models import CodeAgent
from concierge.auth.models import User
from concierge.codeagent.backends import (
    CodeAgentBackend,
    CodeAgentEvent,
    DevinPrintBackend,
    PiBackend,
)
from concierge.codeagent.transport import TransportFactory, open_remote
from concierge.settings import service as settings_service

DEFAULT_TIMEOUT_S = 1800
OUTPUT_MAX = 8000

BackendFactory = Callable[[CodeAgent, TransportFactory], CodeAgentBackend]
OnEvent = Callable[[CodeAgentEvent], Awaitable[None]]


def _make_backend(
    agent: CodeAgent, transport: TransportFactory
) -> CodeAgentBackend:
    args = agent.args or {}
    extra = list(args.get("extra_args") or [])
    if agent.name == "devin":
        return DevinPrintBackend(
            transport, bin=str(args.get("bin") or "devin"), extra_args=extra
        )
    if agent.name == "pi":
        return PiBackend(
            transport, bin=str(args.get("bin") or "pi"), extra_args=extra
        )
    raise ValueError(f"unsupported code agent {agent.name!r}")


def _project_path(
    projects: list[dict[str, Any]], name: str, base: str
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    match = next(
        (p for p in projects if str(p.get("name", "")).lower() == name.lower()),
        None,
    )
    if match is None:
        return None, None, {
            "error": f"unknown project {name!r}",
            "available": [str(p.get("name")) for p in projects],
        }
    path = str(match.get("path") or "")
    norm_base = posixpath.normpath(base)
    if (
        not posixpath.isabs(path)
        or posixpath.normpath(path) != path
        or not (path == norm_base or path.startswith(norm_base + "/"))
    ):
        return None, None, {
            "error": f"project path {path!r} is outside the agent workdir"
        }
    return path, str(match.get("name")), None


async def delegate(
    session: AsyncSession,
    user: User,
    task: str,
    project: str,
    on_event: OnEvent,
    *,
    make_backend: BackendFactory | None = None,
    transport: TransportFactory | None = None,
) -> dict[str, Any]:
    agent = (
        await session.execute(select(CodeAgent).where(CodeAgent.is_default))
    ).scalar_one_or_none()
    if agent is None:
        return {"error": "no default code agent configured"}
    transport = transport or (
        lambda workdir, argv: open_remote(agent.ssh_host, workdir, argv)
    )
    projects = await settings_service.get_global(
        session, "code_agent.projects", []
    )
    if not isinstance(projects, list):
        projects = []
    path, project_name, error = _project_path(projects, project, agent.workdir)
    if error is not None:
        return error
    assert path is not None and project_name is not None

    try:
        backend = (make_backend or _make_backend)(agent, transport)
    except ValueError as e:
        return {"error": str(e)}

    events: list[CodeAgentEvent] = []

    async def run() -> None:
        await backend.start(task, path)
        async for ev in backend.stream_events():
            events.append(ev)
            await on_event(ev)

    timeout_s = float((agent.args or {}).get("timeout_s") or DEFAULT_TIMEOUT_S)
    try:
        await asyncio.wait_for(run(), timeout=timeout_s)
    except TimeoutError:
        await backend.cancel()
        return {
            "ok": False,
            "agent": agent.name,
            "project": project_name,
            "output": "",
            "error": f"code agent timed out after {int(timeout_s)}s",
        }
    except asyncio.CancelledError:
        await backend.cancel()
        raise
    except Exception as e:
        await backend.cancel()
        return {
            "ok": False,
            "agent": agent.name,
            "project": project_name,
            "output": "",
            "error": f"code agent failed: {e}",
        }

    result = next(
        (e for e in reversed(events) if e.kind == "result"),
        CodeAgentEvent("result", "code agent produced no result", ok=False),
    )
    out: dict[str, Any] = {
        "ok": result.ok,
        "agent": agent.name,
        "project": project_name,
        "output": result.text[:OUTPUT_MAX],
    }
    if not result.ok:
        out["error"] = result.text[:1000]
    return out
