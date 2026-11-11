"""SSH transport to the Mac running code-agent CLIs (asyncssh only here).

A non-login SSH PATH lacks homebrew bins, so every remote command is wrapped
as `zsh -lc <script>`; the script echoes its PID first so kill() can signal
the whole process tree's leader.
"""

import asyncio
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

import asyncssh

STDERR_TAIL_MAX = 4000


class RemoteProcess(Protocol):
    """One remote CLI process; stdout consumed line-wise, stderr drained."""

    def lines(self) -> AsyncIterator[str]: ...
    async def write_line(self, line: str) -> None: ...
    async def stdin_eof(self) -> None: ...
    def stderr_tail(self) -> str: ...
    @property
    def exit_status(self) -> int | None: ...
    async def kill(self) -> None: ...


TransportFactory = Callable[[str, list[str]], Awaitable[RemoteProcess]]


class _SshProcess:
    def __init__(
        self,
        conn: asyncssh.SSHClientConnection,
        proc: asyncssh.SSHClientProcess[str],
        pid: int | None,
    ) -> None:
        self._conn = conn
        self._proc = proc
        self._pid = pid
        self._stderr = ""
        self._drain = asyncio.get_running_loop().create_task(
            self._drain_stderr()
        )

    async def lines(self) -> AsyncIterator[str]:
        async for line in self._proc.stdout:
            yield line
        # exit-status and stderr can trail stdout EOF; settle them so callers
        # see the real outcome, then release the connection.
        await self._proc.wait_closed()
        await self._drain
        self._conn.close()

    async def write_line(self, line: str) -> None:
        self._proc.stdin.write(line + "\n")

    async def stdin_eof(self) -> None:
        self._proc.stdin.write_eof()

    def stderr_tail(self) -> str:
        return self._stderr[-1000:]

    @property
    def exit_status(self) -> int | None:
        return self._proc.exit_status

    async def kill(self) -> None:
        try:
            if self._pid is not None:
                await self._conn.run(f"kill -TERM {self._pid}", check=False)
        finally:
            self._conn.close()

    async def _drain_stderr(self) -> None:
        async for chunk in self._proc.stderr:
            self._stderr = (self._stderr + chunk)[-STDERR_TAIL_MAX:]


async def open_remote(
    ssh_host: str, workdir: str, argv: list[str]
) -> RemoteProcess:
    """Spawn `argv` under `workdir` on `ssh_host` (alias in ~/.ssh/config)."""
    conn = await asyncssh.connect(ssh_host)
    script = f"echo $$; cd {shlex.quote(workdir)} && exec {shlex.join(argv)}"
    proc = await conn.create_process(f"zsh -lc {shlex.quote(script)}")
    pid: int | None = None
    try:
        first = await asyncio.wait_for(proc.stdout.readline(), timeout=15)
        pid = int(first.strip())
    except (TimeoutError, ValueError):
        conn.close()
        raise
    return _SshProcess(conn, proc, pid)
