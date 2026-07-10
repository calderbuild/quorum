"""U5: queryable "who changed what when" read path.

`audit_log` is currently only written by `app.events.timetravel.rollback()`
(no other unit writes it yet -- `remember()`/`resolve_conflict()` write to
`memory_events`, not `audit_log`). To make `get_audit_trail` return a real,
useful trail today rather than an almost-empty table, it merges `audit_log`
rows with `memory_events` rows (create/update/conflict_resolve/rollback) for
the requested scope, normalizing both into the same shape and sorting by
timestamp descending. If other units start writing `audit_log` directly
later, those rows show up here for free alongside the merge.
"""

from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.db import run_serializable


async def get_audit_trail(item_id: UUID | None = None, limit: int = 100) -> list[dict]:
    """Return up to `limit` audit entries, newest first.

    Each entry is a dict with at least: actor, action, item_id, detail, ts,
    source ('audit_log' or 'memory_events'). Scoped to `item_id` if given,
    otherwise across all items.
    """

    async def _txn(conn: psycopg.AsyncConnection) -> list[dict]:
        async with conn.cursor(row_factory=dict_row) as cur:
            if item_id is not None:
                await cur.execute(
                    """
                        SELECT entry_id, actor, action, item_id, detail, ts
                        FROM audit_log WHERE item_id = %s
                        ORDER BY ts DESC LIMIT %s
                        """,
                    (item_id, limit),
                )
            else:
                await cur.execute(
                    """
                        SELECT entry_id, actor, action, item_id, detail, ts
                        FROM audit_log ORDER BY ts DESC LIMIT %s
                        """,
                    (limit,),
                )
            audit_rows = await cur.fetchall()
            for r in audit_rows:
                r["source"] = "audit_log"

            if item_id is not None:
                await cur.execute(
                    """
                        SELECT event_id, item_id, op, prev_version, new_version,
                               payload, actor_agent, ts
                        FROM memory_events WHERE item_id = %s
                        ORDER BY ts DESC LIMIT %s
                        """,
                    (item_id, limit),
                )
            else:
                await cur.execute(
                    """
                        SELECT event_id, item_id, op, prev_version, new_version,
                               payload, actor_agent, ts
                        FROM memory_events ORDER BY ts DESC LIMIT %s
                        """,
                    (limit,),
                )
            event_rows = await cur.fetchall()
            for r in event_rows:
                r["source"] = "memory_events"
                r["actor"] = r.pop("actor_agent")
                r["action"] = r.pop("op")
                r["detail"] = r.pop("payload")
                r["entry_id"] = r.pop("event_id")

        merged = sorted(audit_rows + event_rows, key=lambda r: r["ts"], reverse=True)
        return merged[:limit]

    return await run_serializable(_txn)
