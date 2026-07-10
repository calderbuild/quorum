"""U5: time-travel over the append-only `memory_events` log.

Time-travel here is explicitly NOT CockroachDB's native `AS OF SYSTEM TIME`
-- that mechanism is bounded by the cluster's GC TTL (default 25h) and is
unsuitable for the "show me this fact as it looked last week / a year ago"
use case Quorum promises. Instead we replay `memory_events` rows (which are
never deleted or mutated -- `remember()` and `resolve_conflict()` only ever
INSERT into this table) up to a given timestamp or version to reconstruct
historical `MemoryItem` state.

Event payload contract: every `memory_events` row's `payload` JSONB carries
a `"content"` key holding the item's content as of that event (`create`,
`update`, `conflict_resolve`, and `rollback` all follow this convention --
see app/memory/write.py and this module's `rollback()`). If a future event
type is added without a `"content"` key, replay will raise a KeyError on it
rather than silently reconstructing a blank fact -- fail fast, per project
convention.
"""

from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.db import get_pool, run_serializable
from app.llm.embeddings import embed, vector_literal
from app.memory.write import ConflictDetected, _row_to_item
from app.models import MemoryItem


async def _events_for_item(conn: psycopg.AsyncConnection, item_id: UUID) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM memory_events WHERE item_id = %s ORDER BY ts ASC, new_version ASC",
            (item_id,),
        )
        return await cur.fetchall()


def _replay(
    events: list[dict], at_ts: datetime | None, at_version: int | None
) -> dict | None:
    """Find the event that produced the requested state.

    at_version is matched exactly -- versions are a dense, gap-free
    sequence assigned by the CAS in remember()/rollback(), so "version 99
    of an item that only ever reached version 3" is not a valid historical
    point and must not silently resolve to the latest known state.
    at_ts is a cutoff: the last event whose ts is <= at_ts wins.
    """
    if at_version is not None:
        for ev in events:
            if ev["new_version"] == at_version:
                return ev
        return None

    selected = None
    for ev in events:
        if ev["ts"] > at_ts:
            break
        selected = ev
    return selected


async def get_state_at(
    item_id: UUID, at_ts: datetime | None = None, at_version: int | None = None
) -> MemoryItem | None:
    """Reconstruct the item's state as of a timestamp or version.

    Exactly one of at_ts/at_version must be given. Returns None if the item
    has no history yet, or if the cutoff is before the item's first
    ('create') event.
    """
    if (at_ts is None) == (at_version is None):
        raise ValueError("exactly one of at_ts or at_version must be given")

    async def _txn(conn: psycopg.AsyncConnection) -> MemoryItem | None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT scope, kind FROM memory_items WHERE id = %s", (item_id,)
            )
            base = await cur.fetchone()
        if base is None:
            return None

        events = await _events_for_item(conn, item_id)
        if not events:
            return None

        selected = _replay(events, at_ts, at_version)
        if selected is None:
            return None

        create_ts = events[0]["ts"]
        content = selected["payload"]["content"]
        return MemoryItem(
            id=item_id,
            scope=base["scope"],
            kind=base["kind"],
            content=content,
            provenance_agent=selected["actor_agent"],
            version=selected["new_version"],
            valid=True,
            created_at=create_ts,
            updated_at=selected["ts"],
        )

    return await run_serializable(_txn)


async def get_history(item_id: UUID) -> list[dict]:
    """Raw, chronological (oldest first) memory_events rows for an item."""

    async def _txn(conn: psycopg.AsyncConnection) -> list[dict]:
        return await _events_for_item(conn, item_id)

    return await run_serializable(_txn)


async def rollback(item_id: UUID, to_version: int, actor_agent: str) -> MemoryItem:
    """Roll `item_id` back to the content it held at `to_version`.

    Does NOT delete or mutate any history row -- it writes a brand new
    'rollback' event (and an audit_log entry) on top of the log, and
    performs a normal CAS-guarded write to memory_items so the rollback
    itself participates in the same conflict-detection discipline as any
    other write. Raises ConflictDetected (not None) if a concurrent write
    lands on this item between our read and our CAS update -- same
    contract as app.memory.write.remember().

    Manages its own connection/transaction -- does NOT use run_serializable,
    for the same reason remember() doesn't: a CAS miss here is a real
    conflict signal, and auto-retrying would silently clobber whatever the
    concurrent writer just committed.
    """
    historical = await get_state_at(item_id, at_version=to_version)
    if historical is None:
        raise ValueError(f"no history at version {to_version} for item {item_id}")

    vec = vector_literal(await embed(historical.content))
    pool = get_pool()
    async with pool.connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM memory_items WHERE id = %s", (item_id,))
            current_row = await cur.fetchone()
            if current_row is None:
                raise ValueError(f"item {item_id} not found")
            current = _row_to_item(current_row)

            await cur.execute(
                """
                    UPDATE memory_items
                    SET content = %s, embedding = %s, provenance_agent = %s,
                        version = version + 1, updated_at = now()
                    WHERE id = %s AND version = %s
                    RETURNING *
                    """,
                (historical.content, vec, actor_agent, item_id, current.version),
            )
            row = await cur.fetchone()
            if row is None:
                # CAS miss: a concurrent write landed between our read and
                # our update. Surface it as a conflict, never silently
                # overwrite -- same discipline as remember().
                await cur.execute(
                    "SELECT * FROM memory_items WHERE id = %s", (item_id,)
                )
                fresh = await cur.fetchone()
                from app.models import WriteAttempt

                attempt = WriteAttempt(
                    scope=current.scope,
                    kind=current.kind,
                    content=historical.content,
                    agent=actor_agent,
                )
                raise ConflictDetected(item_id, attempt, _row_to_item(fresh))

            item = _row_to_item(row)

            await cur.execute(
                """
                    INSERT INTO memory_events (item_id, op, prev_version, new_version, payload, actor_agent)
                    VALUES (%s, 'rollback', %s, %s, %s, %s)
                    """,
                (
                    item.id,
                    current.version,
                    item.version,
                    psycopg.types.json.Json(
                        {
                            "content": historical.content,
                            "rolled_back_to_version": to_version,
                        }
                    ),
                    actor_agent,
                ),
            )

            await cur.execute(
                """
                    INSERT INTO audit_log (actor, action, item_id, detail)
                    VALUES (%s, 'rollback', %s, %s)
                    """,
                (
                    actor_agent,
                    item.id,
                    psycopg.types.json.Json(
                        {
                            "rolled_back_to_version": to_version,
                            "new_version": item.version,
                            "content": historical.content,
                        }
                    ),
                ),
            )

        return item
