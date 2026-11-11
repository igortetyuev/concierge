"""Code-agent backends: `devin -p` (final answer on stdout) and pi (JSONL)."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from concierge.codeagent.transport import RemoteProcess, TransportFactory

_TEXT_FLUSH = 200
_HEARTBEAT_S = 15.0


@dataclass(slots=True)
class CodeAgentEvent:
    kind: Literal["text", "tool", "result"]
    text: str
    ok: bool = True


class CodeAgentBackend(Protocol):
    name: str

    async def start(self, task: str, workdir: str) -> None: ...
    def stream_events(self) -> AsyncIterator[CodeAgentEvent]: ...
    async def cancel(self) -> None: ...


class _LineAggregator:
    """Aggregates streamed text chunks into ~line/sentence-size events."""

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, chunk: str) -> str | None:
        self.buf += chunk
        cut = self.buf.rfind("\n") + 1
        if len(self.buf) >= _TEXT_FLUSH and cut < len(self.buf) // 2:
            cut = _TEXT_FLUSH
        if not cut:
            return None
        out, self.buf = self.buf[:cut], self.buf[cut:]
        return out

    def flush(self) -> str | None:
        out, self.buf = self.buf, ""
        return out or None


class DevinPrintBackend:
    """`devin -p`: uses local `devin auth login` creds, prints only the
    final answer to stdout at the end (no stream — heartbeat instead)."""

    name = "devin"

    def __init__(
        self,
        transport_factory: TransportFactory,
        bin: str = "devin",
        extra_args: list[str] | None = None,
    ) -> None:
        self._factory = transport_factory
        self._argv = [
            bin,
            "-p",
            "--permission-mode",
            "dangerous",
            "--respect-workspace-trust",
            "false",
            *(extra_args or []),
        ]
        self._proc: RemoteProcess | None = None

    async def start(self, task: str, workdir: str) -> None:
        self._proc = await self._factory(workdir, [*self._argv, "--", task])

    async def stream_events(self) -> AsyncIterator[CodeAgentEvent]:
        assert self._proc is not None
        lines: list[str] = []
        started = time.monotonic()
        it = self._proc.lines()
        step: asyncio.Task[str] | None = None
        while True:
            if step is None:
                step = asyncio.ensure_future(it.__anext__())
            done, _ = await asyncio.wait({step}, timeout=_HEARTBEAT_S)
            if not done:
                elapsed = int(time.monotonic() - started)
                yield CodeAgentEvent(
                    "text",
                    f"devin работает… {elapsed // 60}:{elapsed % 60:02d}",
                )
                continue
            try:
                lines.append(step.result())
                step = None
            except StopAsyncIteration:
                break
        status = self._proc.exit_status
        if status == 0:
            yield CodeAgentEvent("result", "".join(lines).strip(), ok=True)
        else:
            tail = self._proc.stderr_tail().strip()
            yield CodeAgentEvent(
                "result", tail or f"devin exited (status={status})", ok=False
            )

    async def cancel(self) -> None:
        if self._proc is not None:
            await self._proc.kill()


class PiBackend:
    """`pi -p --mode json --no-session`: JSONL events on stdout."""

    name = "pi"

    def __init__(
        self,
        transport_factory: TransportFactory,
        bin: str = "pi",
        extra_args: list[str] | None = None,
    ) -> None:
        self._factory = transport_factory
        self._argv = [bin, "--mode", "json", "--no-session", "-p", *(extra_args or [])]
        self._proc: RemoteProcess | None = None
        self._early_error: str | None = None

    async def start(self, task: str, workdir: str) -> None:
        self._proc = await self._factory(workdir, self._argv)
        await self._proc.write_line(task)
        await self._proc.stdin_eof()

    async def stream_events(self) -> AsyncIterator[CodeAgentEvent]:
        if self._early_error:
            yield CodeAgentEvent("result", self._early_error, ok=False)
            return
        assert self._proc is not None
        agg = _LineAggregator()
        final_text = ""
        stop_reason = ""
        aborted = False
        async for line in self._proc.lines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "message_update":
                ev = msg.get("assistantMessageEvent") or {}
                if ev.get("type") == "text_delta":
                    out = agg.feed(str(ev.get("delta") or ""))
                    if out:
                        yield CodeAgentEvent("text", out)
            elif mtype == "tool_execution_start":
                yield CodeAgentEvent("tool", _tool_line(msg))
            elif mtype == "message_end":
                m = msg.get("message") or {}
                if m.get("role") == "assistant":
                    final_text = "".join(
                        str(p.get("text") or "")
                        for p in m.get("content") or []
                        if p.get("type") == "text"
                    )
                    stop_reason = str(m.get("stopReason") or "")
            elif mtype == "agent_settled":
                aborted = bool(msg.get("aborted"))
        rest = agg.flush()
        if rest:
            yield CodeAgentEvent("text", rest)
        status = self._proc.exit_status
        tail = self._proc.stderr_tail().strip()
        if aborted or (status not in (0, None)):
            error = tail or f"pi exited (status={status})"
            yield CodeAgentEvent("result", error, ok=False)
        elif stop_reason and stop_reason not in ("stop", "end_turn"):
            yield CodeAgentEvent("result", f"stopReason={stop_reason}", ok=False)
        else:
            yield CodeAgentEvent("result", final_text, ok=True)

    async def cancel(self) -> None:
        if self._proc is not None:
            await self._proc.kill()


def _tool_line(msg: dict[str, Any]) -> str:
    name = str(msg.get("toolName") or "tool")
    args = msg.get("args") or {}
    brief = ""
    if isinstance(args, dict) and args:
        first = next(iter(args.values()))
        brief = f": {str(first)[:120]}"
    return f"{name}{brief}"
