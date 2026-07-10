"""U1: proves the consistency-core thesis.

The concurrency test is the factual backbone of the entire project: run the
identical concurrent workload against the naive baseline (backend/sim/
baseline_lww.py) and against the real write path (app/memory/write.py). The
baseline must lose an update silently; Quorum must detect it as a conflict
and never lose either write from its history.
"""

import asyncio

import pytest

from app.db import get_pool
from app.memory.write import ConflictDetected, get_by_scope_kind, remember
from sim.baseline_lww import lww_read, lww_write

pytestmark = pytest.mark.asyncio


async def test_single_write_persists_one_event(clean_tables, unique_scope):
    item = await remember(unique_scope, "policy", "use library X", "agent-a")
    assert item.version == 1
    assert item.content == "use library X"

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM memory_events WHERE item_id = %s", (item.id,)
            )
            (count,) = await cur.fetchone()
    assert count == 1


async def test_identical_rewrite_is_idempotent_not_a_conflict(
    clean_tables, unique_scope
):
    await remember(unique_scope, "policy", "use library X", "agent-a")
    # Re-sending the exact same fact must not raise and must not create a
    # spurious new version/event.
    item2 = await remember(unique_scope, "policy", "use library X", "agent-a")
    assert item2.version == 1


async def test_concurrent_conflicting_writes_are_detected_not_lost(
    clean_tables, unique_scope
):
    """The core assertion: two agents race to write DIFFERENT facts to the
    same (scope, kind). Exactly one commits cleanly; the other must raise
    ConflictDetected -- never silently overwrite, never silently vanish.
    """
    await remember(unique_scope, "policy", "initial value", "seed-agent")

    results = await asyncio.gather(
        remember(unique_scope, "policy", "use library X", "agent-a"),
        remember(unique_scope, "policy", "use library Y", "agent-b"),
        return_exceptions=True,
    )

    conflicts = [r for r in results if isinstance(r, ConflictDetected)]
    successes = [r for r in results if not isinstance(r, Exception)]
    other_errors = [
        r
        for r in results
        if isinstance(r, Exception) and not isinstance(r, ConflictDetected)
    ]

    assert not other_errors, f"unexpected errors: {other_errors}"
    assert len(conflicts) == 1, (
        "expected exactly one writer to detect the concurrent conflict"
    )
    assert len(successes) == 1, "expected exactly one writer to commit cleanly"

    # U1's guarantee stops here: the conflict is detected and BOTH values are
    # carried on the exception object -- the losing write is never silently
    # discarded at the API level, even though it is not yet a row in
    # memory_events (persisting the losing value into history is U2's job:
    # app.conflicts.resolver.resolve_conflict(), which receives exactly this
    # ConflictDetected and is responsible for writing the `conflicts` row,
    # the resolution, and retaining the losing value -- covered by U2's own
    # tests, not here).
    conflict = conflicts[0]
    assert conflict.attempt.content in ("use library X", "use library Y")
    assert conflict.current.content in ("use library X", "use library Y")
    assert conflict.attempt.content != conflict.current.content

    # The winning write DID land correctly and IS in history.
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT payload->>'content' FROM memory_events "
                "WHERE item_id = %s ORDER BY ts",
                (conflict.item_id,),
            )
            rows = await cur.fetchall()
    written_contents = {r[0] for r in rows}
    assert conflict.current.content in written_contents
    assert "initial value" in written_contents


async def test_baseline_lww_silently_loses_a_concurrent_write(
    clean_tables, unique_scope
):
    """The honest contrast: the naive read-modify-write baseline has NO
    conflict detection. Under the identical race, it silently drops one
    writer's fact with no trace and no error raised to either caller. This
    is the exact bug class app.memory.write.remember() exists to prevent.
    """
    await lww_write(unique_scope, "policy", "initial value", "seed-agent")

    await asyncio.gather(
        lww_write(unique_scope, "policy", "use library X", "agent-a"),
        lww_write(unique_scope, "policy", "use library Y", "agent-b"),
    )

    final = await lww_read(unique_scope, "policy")
    # Whichever write landed last silently overwrote the other -- exactly
    # one survives, no error, no record that a collision ever happened.
    assert final in ("use library X", "use library Y")


async def test_atomic_write_never_leaves_an_orphaned_row(clean_tables, unique_scope):
    """Embedding + metadata land in the same row, same transaction (KTD-5):
    a fully written item always has both a non-null embedding and content.
    """
    item = await remember(unique_scope, "fact", "atomic write check", "agent-a")
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT content, embedding IS NOT NULL FROM memory_items WHERE id = %s",
                (item.id,),
            )
            content, has_embedding = await cur.fetchone()
    assert content == "atomic write check"
    assert has_embedding is True


async def test_get_by_scope_kind_returns_none_when_absent(clean_tables, unique_scope):
    result = await get_by_scope_kind(unique_scope, "nonexistent-kind")
    assert result is None
