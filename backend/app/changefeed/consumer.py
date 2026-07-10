"""U4: changefeed consumer -- real CockroachDB CDC, not a poll loop.

Uses `CREATE CHANGEFEED FOR TABLE ... WITH updated` -- a core/sinkless
changefeed (no enterprise license, no external sink required). This is a
genuine SQL statement that never completes: once executed it streams one row
per change as CockroachDB's rangefeed picks it up. psycopg3's
`AsyncCursor.stream()` is built for exactly this shape (iterate rows as they
arrive over the wire rather than materializing a finite result set), so we
use it as the transport for a live async generator of change events.

This is NOT the documented polling fallback -- it is the real changefeed,
live-verified against this cluster (see infra/crdb/init.sql's header comment
and `backend/tests/test_changefeed.py`). If a future change to the cluster
or timeline ever forces a fallback to polling `memory_events`/`conflicts`
`WHERE ts > $last_seen`, that fallback must be implemented in this same
module with an explicit docstring note -- it must never masquerade as this
function.

Each watched table's changefeed row on the wire is `(table_name, key, value)`
where `key` is a JSON-encoded primary key array and `value` is a JSON object
shaped like `{"after": {...columns...}, "updated": "<hlc timestamp>"}` (a
deleted row has `"after": null`). `changefeed_events()` normalizes that into
a flat dict per event.

A changefeed query holds its connection open indefinitely, so it deliberately
does NOT borrow from the shared `app.db.get_pool()` pool -- doing so would
either starve the pool (max_size=10) or never release a connection back to
it. Instead it opens one dedicated, un-pooled, autocommit connection for the
lifetime of the generator and closes it when the consumer stops iterating
(caller cancels the task / calls `.aclose()` on the generator).
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import psycopg

from app.config import get_settings

# Tables the demo cares about watching live, per ARCHITECTURE.md's U4 contract.
WATCHED_TABLES = ("memory_items", "memory_events", "conflicts")


async def changefeed_events(
    tables: tuple[str, ...] = WATCHED_TABLES,
) -> AsyncIterator[dict[str, Any]]:
    """Yield a dict per row change on `tables`, forever, until cancelled.

    Each yielded dict: {"table": str, "key": list, "after": dict | None,
    "updated": str | None}. `after` is None for a deleted row (not expected
    in this schema today -- everything is soft-invalidated via `valid`, not
    DELETEd -- but the shape is handled defensively regardless).
    """
    settings = get_settings()
    conn = await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True)
    try:
        cur = conn.cursor()
        query = f"CREATE CHANGEFEED FOR TABLE {', '.join(tables)} WITH updated"
        async for table, key, value in cur.stream(query):
            payload = json.loads(value) if value is not None else {}
            yield {
                "table": table,
                "key": json.loads(key) if key is not None else None,
                "after": payload.get("after"),
                "updated": payload.get("updated"),
            }
    finally:
        await conn.close()
