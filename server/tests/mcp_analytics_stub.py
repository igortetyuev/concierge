"""Minimal stdio MCP analytics stub used by tests.

Only `import_rows` is implemented; it returns the same structured success
shape as the real analytics MCP importer without touching a database.
"""

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

server = FastMCP("analytics")


@server.tool()
def import_rows(
    table: str,
    rows: list[dict[str, Any]],
    mode: Literal["append", "replace"] = "append",
) -> dict[str, Any]:
    """Pretend to import rows into the analytics sandbox schema."""
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return {
        "table": f"sandbox.{table}",
        "inserted": len(rows),
        "columns": columns,
    }


if __name__ == "__main__":
    server.run(transport="stdio")
