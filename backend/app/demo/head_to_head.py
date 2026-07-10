"""U6: the head-to-head demo -- the single most important artifact in the
whole project.

Runs the IDENTICAL concurrent-write race against both sides of the thesis:

- The naive baseline (`sim.baseline_lww`): a faithful read-modify-write store
  with no version guard. Under a genuine race, one of the two concurrent
  facts is silently overwritten -- no error, no trace, no way to know a
  conflict ever happened. `run_head_to_head` does not have access to any
  hidden "which write actually landed last" signal either -- it infers the
  lost value exactly the way an external observer would: whichever of
  `fact_a`/`fact_b` is NOT the final baseline value is, by construction, the
  one that got clobbered. That inference-by-elimination (rather than any
  trace surviving in `baseline_items` itself) IS the point being
  demonstrated.

- Quorum (`app.memory.write.remember` + `app.conflicts.resolver`): the same
  race is detected via optimistic version CAS inside CockroachDB SERIALIZABLE
  transactions. The losing writer raises `ConflictDetected` rather than
  silently overwriting; `resolve_conflict()` reconciles it into a new
  version, and both original values remain fully recoverable -- one as the
  resolution's chosen value / current row, the other verbatim in
  `conflicts.resolution.candidates` and in the `memory_events` history.

Both sides run the exact same workload driver (`sim.fleet.run_concurrent_writes`)
against a fresh seed value, so the comparison is apples-to-apples.
"""

from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.conflicts.resolver import resolve_conflict
from app.db import get_pool
from app.events.timetravel import get_history
from app.memory.write import ConflictDetected, get_by_scope_kind, remember
from sim.baseline_lww import lww_read, lww_write, reset_baseline
from sim.fleet import run_concurrent_writes


async def _clean_quorum_scope(scope: str) -> None:
    """Scoped equivalent of tests/conftest.py's `clean_tables`, limited to
    `scope` so the demo doesn't clobber unrelated data living in the shared
    dev cluster (unlike `clean_tables`, which truncates whole tables and is
    only safe inside an isolated test run).
    """
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM memory_events
                WHERE item_id IN (SELECT id FROM memory_items WHERE scope = %s)
                """,
                (scope,),
            )
            await cur.execute(
                """
                DELETE FROM conflicts
                WHERE item_id IN (SELECT id FROM memory_items WHERE scope = %s)
                """,
                (scope,),
            )
            await cur.execute("DELETE FROM memory_items WHERE scope = %s", (scope,))
        await conn.commit()


async def _run_baseline_race(
    scope: str, kind: str, seed_content: str, fact_a: str, fact_b: str
) -> dict[str, Any]:
    await reset_baseline()
    await lww_write(scope, kind, seed_content, "seed-agent")

    await run_concurrent_writes(
        scope,
        kind,
        [(fact_a, "agent-a"), (fact_b, "agent-b")],
        lww_write,
    )

    final_value = await lww_read(scope, kind)
    # No trace of the loser survives in the baseline's own data -- that IS
    # the bug being demonstrated. We can only infer it by elimination: of
    # the two facts raced in, whichever one is NOT the final value must have
    # been silently clobbered.
    lost_value = fact_b if final_value == fact_a else fact_a

    return {
        "final_value": final_value,
        "lost_value": lost_value,
        "conflict_detected": False,
    }


async def _run_quorum_race(
    scope: str, kind: str, seed_content: str, fact_a: str, fact_b: str
) -> dict[str, Any]:
    await _clean_quorum_scope(scope)
    await remember(scope, kind, seed_content, "seed-agent")

    results = await run_concurrent_writes(
        scope,
        kind,
        [(fact_a, "agent-a"), (fact_b, "agent-b")],
        remember,
    )

    conflicts = [r for r in results if isinstance(r, ConflictDetected)]
    other_errors = [
        r
        for r in results
        if isinstance(r, Exception) and not isinstance(r, ConflictDetected)
    ]
    if other_errors:
        raise other_errors[0]
    if len(conflicts) != 1:
        raise RuntimeError(
            f"expected exactly one ConflictDetected from the race, got {len(conflicts)}"
        )

    write_result = await resolve_conflict(conflicts[0], policy="merge")

    current = await get_by_scope_kind(scope, kind)
    history = await get_history(current.id)

    return {
        "final_value": write_result.item.content,
        "conflict": write_result.conflict.model_dump()
        if write_result.conflict
        else None,
        "history": history,
        "conflict_detected": True,
    }


async def run_head_to_head(
    scope: str, kind: str, seed_content: str, fact_a: str, fact_b: str
) -> dict[str, Any]:
    """Run the identical concurrent-write race against both the naive
    baseline and Quorum, and return a structured side-by-side comparison.
    """
    baseline = await _run_baseline_race(scope, kind, seed_content, fact_a, fact_b)
    quorum = await _run_quorum_race(scope, kind, seed_content, fact_a, fact_b)

    return {
        "scope": scope,
        "kind": kind,
        "seed_content": seed_content,
        "fact_a": fact_a,
        "fact_b": fact_b,
        "baseline": baseline,
        "quorum": quorum,
    }
