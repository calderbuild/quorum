"""The naive comparison store for the head-to-head demo.

This is a faithful implementation of how a bolt-on, non-transactional memory
layer (the Mem0-style pattern: read current state, decide, write) behaves
under concurrent writes: a plain read-modify-write with NO version check and
NO conflict detection. Whichever writer's UPDATE commits last simply
overwrites, silently. This is not a strawman -- it is the literal shape of
"read current value, then write" without a database that can enforce
optimistic concurrency, which is exactly what plain vector stores cannot do.

Contrast with app/memory/write.py's `remember()`, which detects the identical
race via version CAS and routes it to conflict resolution instead.
"""

from psycopg.rows import dict_row

from app.db import get_pool


async def lww_write(scope: str, kind: str, content: str, agent: str) -> None:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # Read current value (simulating an agent deciding what to write
            # based on what it currently believes is true)...
            await cur.execute(
                "SELECT content FROM baseline_items WHERE scope = %s AND kind = %s",
                (scope, kind),
            )
            await cur.fetchone()

            # ...then blindly write, with no check that the value we read is
            # still the current value. If another writer's transaction
            # landed in between, this UPDATE silently clobbers it -- the
            # lost-update bug.
            await cur.execute(
                """
                INSERT INTO baseline_items (scope, kind, content, provenance_agent)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (scope, kind)
                DO UPDATE SET content = excluded.content,
                              provenance_agent = excluded.provenance_agent,
                              updated_at = now()
                """,
                (scope, kind, content, agent),
            )
        await conn.commit()


async def lww_read(scope: str, kind: str) -> str | None:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT content FROM baseline_items WHERE scope = %s AND kind = %s",
                (scope, kind),
            )
            row = await cur.fetchone()
            return row["content"] if row else None


async def reset_baseline() -> None:
    """Test/demo helper: clear the baseline table between runs."""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM baseline_items")
        await conn.commit()
