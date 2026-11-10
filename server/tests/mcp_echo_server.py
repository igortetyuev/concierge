"""Minimal stdio MCP server used by tests (one tool: echo)."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("echo")


@server.tool()
def echo(text: str) -> str:
    """Echo back the input text."""
    return f"echo:{text}"


if __name__ == "__main__":
    server.run(transport="stdio")
