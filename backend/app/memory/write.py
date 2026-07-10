"""U1: the consistency core -- the load-bearing wall of the whole thesis.

Concurrent writers to the same (scope, kind) key are never silently
overwritten. `remember()` uses optimistic version CAS (UPDATE ... WHERE
version=$N) inside a CockroachDB SERIALIZABLE transaction: if a concurrent
write already landed between our read and our write attempt, the CAS misses
(0 rows updated) and we raise a Conflict rather than blindly retrying and
clobbering the other agent's committed fact.
"""

import asyncio
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from app.db import get_pool, run_serializable
from app.llm.embeddings import embed, vector_literal
from app.models import MemoryItem, WriteAttempt


class ConflictDetected(Exception):
    """A concurrent write landed between our read and our CAS update.

    Carries enough context for app.conflicts.resolver.resolve_conflict() to
    reconcile: the write we attempted, and the row as it currently stands.
    """

    def __init__(self, item_id: UUID, attempt: WriteAttempt, current: MemoryItem):
        self.item_id = item_id
        self.attempt = attempt
        self.current = current
        super().__init__(
            f"conflict on item {item_id}: concurrent write already at version {current.version}"
        )


def _row_to_item(row: dict) -> MemoryItem:
    return MemoryItem(
        id=row["id"],
        scope=row["scope"],
        kind=row["kind"],
        content=row["content"],
        provenance_agent=row["provenance_agent"],
        version=row["version"],
        valid=row["valid"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def remember(scope: str, kind: str, content: str, agent: str) -> MemoryItem:
    """Write a fact.

    Raises ConflictDetected if a concurrent write already landed. Callers
    that want auto-resolution should catch this and call
    app.conflicts.resolver.resolve_conflict() -- never retry remember()
    blindly, since a blind retry would silently overwrite the other agent's
    fact, exactly the bug this system exists to prevent.

    Deliberately does NOT use db.run_serializable's auto-retry-on-40001: a
    SerializationFailure here means CockroachDB's own SSI detected that a
    concurrent transaction committed a conflicting write to this row between
    our read and our write. That IS the conflict signal -- blindly retrying
    would re-read the now-committed value and silently overwrite it, which
    is exactly the lost-update bug this function exists to prevent (this was
    caught live: an earlier version auto-retried here and both concurrent
    writers "succeeded", clobbering one fact with zero trace of the race).
    """
    vec = vector_literal(await embed(content))
    attempt = WriteAttempt(scope=scope, kind=kind, content=content, agent=agent)
    pool = get_pool()

    try:
        async with pool.connection() as conn, conn.transaction():
            return await _remember_txn(conn, scope, kind, content, vec, agent, attempt)
    except psycopg.errors.SerializationFailure:
        current = await get_by_scope_kind(scope, kind)
        if current is None:
            # The conflicting transaction's write hasn't become visible to a
            # fresh read yet (rare timing edge); one bounded retry of the
            # READ (never the write) is safe here.
            await asyncio.sleep(0.02)
            current = await get_by_scope_kind(scope, kind)
        if current is None:
            raise
        raise ConflictDetected(current.id, attempt, current) from None


async def _remember_txn(
    conn: psycopg.AsyncConnection,
    scope: str,
    kind: str,
    content: str,
    vec: str,
    agent: str,
    attempt: WriteAttempt,
) -> MemoryItem:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM memory_items WHERE scope = %s AND kind = %s",
            (scope, kind),
        )
        existing = await cur.fetchone()

        if existing is None:
            try:
                await cur.execute(
                    """
                        INSERT INTO memory_items (scope, kind, content, embedding, provenance_agent)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING *
                        """,
                    (scope, kind, content, vec, agent),
                )
                row = await cur.fetchone()
                item = _row_to_item(row)
                await cur.execute(
                    """
                        INSERT INTO memory_events (item_id, op, prev_version, new_version, payload, actor_agent)
                        VALUES (%s, 'create', NULL, %s, %s, %s)
                        """,
                    (item.id, item.version, Json({"content": content}), agent),
                )
                return item
            except psycopg.errors.UniqueViolation:
                # A concurrent writer created (scope, kind) between our
                # SELECT and our INSERT. Re-read and surface as a
                # conflict rather than silently losing our own write.
                await cur.execute(
                    "SELECT * FROM memory_items WHERE scope = %s AND kind = %s",
                    (scope, kind),
                )
                row = await cur.fetchone()
                current = _row_to_item(row)
                raise ConflictDetected(current.id, attempt, current) from None

        current = _row_to_item(existing)

        if current.content == content:
            # Identical fact re-sent -- idempotent no-op, not a conflict.
            return current

        await cur.execute(
            """
                UPDATE memory_items
                SET content = %s, embedding = %s, provenance_agent = %s,
                    version = version + 1, updated_at = now()
                WHERE id = %s AND version = %s
                RETURNING *
                """,
            (content, vec, agent, current.id, current.version),
        )
        row = await cur.fetchone()

        if row is None:
            # CAS miss: a concurrent write already committed and moved
            # the version out from under us. Re-read the now-current row
            # and hand both versions to the caller -- never overwrite.
            await cur.execute("SELECT * FROM memory_items WHERE id = %s", (current.id,))
            fresh = await cur.fetchone()
            raise ConflictDetected(current.id, attempt, _row_to_item(fresh))

        item = _row_to_item(row)
        await cur.execute(
            """
                INSERT INTO memory_events (item_id, op, prev_version, new_version, payload, actor_agent)
                VALUES (%s, 'update', %s, %s, %s, %s)
                """,
            (
                item.id,
                current.version,
                item.version,
                Json({"content": content}),
                agent,
            ),
        )
        return item


async def get_by_scope_kind(scope: str, kind: str) -> MemoryItem | None:
    async def _txn(conn: psycopg.AsyncConnection) -> MemoryItem | None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM memory_items WHERE scope = %s AND kind = %s",
                (scope, kind),
            )
            row = await cur.fetchone()
            return _row_to_item(row) if row else None

    return await run_serializable(_txn)
