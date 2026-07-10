"""U6a: MCP server exposing Quorum's write/retrieve/conflict-inspection paths
as MCP tools, so any MCP-capable agent (Claude Desktop, etc.) can read and
write shared memory through the exact same consistency-checked path the demo
uses -- no separate/looser code path for "agent access."

Tools:
  - remember: wraps app.memory.write.remember(). On a detected concurrent
    write, does NOT swallow it -- returns the conflict as structured JSON
    (item_id, current version/content, the attempt that lost the race) so
    the calling agent can see the race happened instead of it vanishing
    silently. This mirrors the "never silently overwrite" thesis at the
    tool-call boundary too.
  - retrieve: wraps app.memory.retrieve.retrieve().
  - get_conflicts: small read query over the `conflicts` table, scoped by
    item_id (exact) or scope (joins through memory_items, since `conflicts`
    itself has no scope column).

Transport: stdio, per the standard pattern for this SDK version (mcp==1.1.3,
predates the FastMCP high-level API -- this uses the low-level
mcp.server.Server directly: @server.list_tools() / @server.call_tool()
decorators, then server.run() over mcp.server.stdio.stdio_server()).
"""

import asyncio
import json
from typing import Any
from uuid import UUID

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from psycopg.rows import dict_row

from app.db import close_pool, open_pool, run_serializable
from app.memory.retrieve import retrieve as retrieve_memory
from app.memory.write import ConflictDetected, remember as remember_memory

server = Server("quorum")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, UUID):
        return str(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


async def get_conflicts(
    item_id: str | None = None, scope: str | None = None
) -> list[dict[str, Any]]:
    """Read `conflicts` rows, scoped by item_id (exact) or scope (joins
    through memory_items -- `conflicts` itself carries no scope column).

    Read-only, naturally idempotent -- goes through run_serializable like
    retrieve.py does.
    """
    if not item_id and not scope:
        raise ValueError("get_conflicts requires item_id or scope")

    async def _txn(conn):
        async with conn.cursor(row_factory=dict_row) as cur:
            if item_id:
                await cur.execute(
                    """
                    SELECT conflict_id, item_id, version_a, version_b, policy,
                           resolution, rationale, status, ts
                    FROM conflicts
                    WHERE item_id = %s
                    ORDER BY ts DESC
                    """,
                    (item_id,),
                )
            else:
                await cur.execute(
                    """
                    SELECT c.conflict_id, c.item_id, c.version_a, c.version_b,
                           c.policy, c.resolution, c.rationale, c.status, c.ts
                    FROM conflicts c
                    JOIN memory_items m ON m.id = c.item_id
                    WHERE m.scope = %s
                    ORDER BY c.ts DESC
                    """,
                    (scope,),
                )
            return await cur.fetchall()

    return await run_serializable(_txn)


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="remember",
            description=(
                "Write a fact to Quorum's shared memory. Raises/reports a "
                "conflict instead of silently overwriting if a concurrent "
                "write already landed on the same (scope, kind)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "description": "Memory scope/namespace",
                    },
                    "kind": {
                        "type": "string",
                        "description": "Fact kind/key within scope",
                    },
                    "content": {"type": "string", "description": "The fact's content"},
                    "agent": {
                        "type": "string",
                        "description": "Provenance: which agent is writing",
                    },
                },
                "required": ["scope", "kind", "content", "agent"],
            },
        ),
        types.Tool(
            name="retrieve",
            description=(
                "Semantic (ANN) retrieval of the top-k memory items in a "
                "scope nearest to a query string."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scope": {"type": "string"},
                    "query": {"type": "string"},
                    "k": {"type": "integer", "default": 5},
                },
                "required": ["scope", "query"],
            },
        ),
        types.Tool(
            name="get_conflicts",
            description=(
                "List recorded conflicts, scoped by item_id (exact) or "
                "scope (all conflicts on items in that scope)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Exact memory item UUID",
                    },
                    "scope": {
                        "type": "string",
                        "description": "Memory scope/namespace",
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any]
) -> list[types.TextContent]:
    if name == "remember":
        try:
            item = await remember_memory(
                scope=arguments["scope"],
                kind=arguments["kind"],
                content=arguments["content"],
                agent=arguments["agent"],
            )
            result = {"status": "ok", "item": item.model_dump()}
        except ConflictDetected as exc:
            result = {
                "status": "conflict",
                "item_id": str(exc.item_id),
                "attempt": exc.attempt.model_dump(),
                "current": exc.current.model_dump(),
            }
    elif name == "retrieve":
        items = await retrieve_memory(
            scope=arguments["scope"],
            query=arguments["query"],
            k=arguments.get("k", 5),
        )
        result = {"items": [item.model_dump() for item in items]}
    elif name == "get_conflicts":
        conflicts = await get_conflicts(
            item_id=arguments.get("item_id"), scope=arguments.get("scope")
        )
        result = {"conflicts": conflicts}
    else:
        raise ValueError(f"unknown tool: {name}")

    return [
        types.TextContent(type="text", text=json.dumps(result, default=_json_default))
    ]


async def main() -> None:
    await open_pool()
    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="quorum",
                    server_version="0.1.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
