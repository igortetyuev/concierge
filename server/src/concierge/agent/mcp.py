import contextlib
import logging
import shlex
from dataclasses import dataclass
from typing import Any, Self

from fastmcp.client.transports import (
    SSETransport,
    StdioTransport,
    StreamableHttpTransport,
)
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset, PrefixedToolset, WrapperToolset
from pydantic_ai.toolsets.filtered import FilteredToolset
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.agent import providers
from concierge.agent.models import McpServer
from concierge.auth.models import User

log = logging.getLogger(__name__)

# Stdio servers reached over SSH (browserdata) need a few seconds to spawn;
# pydantic-ai's default of 5s is too tight for a waking machine.
MCP_INIT_TIMEOUT = 15.0


@dataclass
class TolerantToolset(WrapperToolset[Any]):
    """Drops the wrapped toolset from the run when it fails to connect.

    A single unreachable MCP server (asleep/off host, dead endpoint) must not
    fail the whole agent run — it just loses that server's tools.
    """

    _down: bool = False

    async def __aenter__(self) -> Self:
        try:
            await self.wrapped.__aenter__()
        except Exception:
            self._down = True
            log.warning(
                "mcp toolset %r failed to connect; running without it",
                self.wrapped.label,
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await self.wrapped.__aexit__(None, None, None)
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        if self._down:
            return None
        return await self.wrapped.__aexit__(*args)

    async def get_tools(self, ctx: Any) -> dict[str, Any]:
        if self._down:
            return {}
        return await self.wrapped.get_tools(ctx)


def _allowed_server_names(permissions: dict[str, Any]) -> set[str] | None:
    """None = all servers allowed; a set = explicit allowlist."""
    if permissions.get("*"):
        return None
    conf = permissions.get("mcp_servers")
    if not isinstance(conf, list):
        return set()
    if "*" in conf:
        return None
    return set(conf)


def _build_server(
    srv: McpServer, env: dict[str, str]
) -> AbstractToolset[Any] | None:
    if srv.transport == "stdio":
        if not srv.command:
            return None
        argv = shlex.split(srv.command)
        if not argv:
            return None
        return MCPToolset(
            StdioTransport(argv[0], args=argv[1:], env=env or None),
            init_timeout=MCP_INIT_TIMEOUT,
        )
    if srv.transport in ("http", "streamable_http") and srv.url:
        return MCPToolset(
            StreamableHttpTransport(srv.url), init_timeout=MCP_INIT_TIMEOUT
        )
    if srv.transport == "sse" and srv.url:
        return MCPToolset(SSETransport(srv.url), init_timeout=MCP_INIT_TIMEOUT)
    log.warning("mcp server %r: bad transport/url", srv.name)
    return None


def _tool_filter(srv: McpServer) -> Any:
    allowed = set(srv.allowed_tools or [])
    if not allowed or "*" in allowed:
        return None  # no restriction

    def keep(_ctx: Any, tool_def: Any) -> bool:
        name = str(tool_def.name)
        return name in allowed or name.removeprefix(f"{srv.name}_") in allowed

    return keep


async def build_toolsets(
    session: AsyncSession, user: User
) -> list[AbstractToolset[Any]]:
    """MCP toolsets for a run, filtered by role (mcp_servers) and allowed_tools."""
    allowed_names = _allowed_server_names(user.role.permissions)
    if allowed_names == set():
        return []
    rows = (
        await session.execute(
            select(McpServer).where(McpServer.enabled).order_by(McpServer.id)
        )
    ).scalars()
    toolsets: list[AbstractToolset[Any]] = []
    for srv in rows:
        if allowed_names is not None and srv.name not in allowed_names:
            continue
        env = await providers.resolve_ref_dict(session, user, srv.env_ref)
        toolset = _build_server(srv, env)
        if toolset is None:
            continue
        prefixed: AbstractToolset[Any] = PrefixedToolset(toolset, srv.name)
        keep = _tool_filter(srv)
        if keep is not None:
            prefixed = FilteredToolset(prefixed, keep)
        toolsets.append(TolerantToolset(prefixed))
    return toolsets
