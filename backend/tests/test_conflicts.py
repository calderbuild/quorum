"""U2: proves conflict resolution is non-lossy and durably recorded.

Every test here starts from a genuine ConflictDetected raised by
app.memory.write.remember() under a real concurrent race (per
ARCHITECTURE.md's testing conventions -- concurrency-correctness tests must
use asyncio.gather(..., return_exceptions=True) and assert on the exact
count of successes vs. ConflictDetected instances), then feeds it into
resolve_conflict() and inspects both the returned WriteResult and the raw
`conflicts` / `memory_events` rows.
"""

import asyncio

import pytest

from app.conflicts.resolver import resolve_conflict
from app.db import get_pool
from app.memory.write import ConflictDetected, remember

pytestmark = pytest.mark.asyncio


async def _provoke_conflict(scope: str) -> ConflictDetected:
    """Race two writers to the same (scope, kind) and return the
    ConflictDetected raised by the loser -- never synthesize one by hand.
    """
    await remember(scope, "policy", "initial value", "seed-agent")

    results = await asyncio.gather(
        remember(scope, "policy", "use library X", "agent-a"),
        remember(scope, "policy", "use library Y", "agent-b"),
        return_exceptions=True,
    )
    conflicts = [r for r in results if isinstance(r, ConflictDetected)]
    assert len(conflicts) == 1, "expected exactly one ConflictDetected from the race"
    return conflicts[0]


async def test_adjudicate_policy_resolves_and_persists(clean_tables, unique_scope):
    conflict = await _provoke_conflict(unique_scope)

    result = await resolve_conflict(conflict, policy="adjudicate")

    # sim-mode adjudicate is a longer/more-specific heuristic -- both
    # candidate contents are the same length here ("use library X" / "use
    # library Y"), so the result must be exactly one of them, chosen
    # deterministically, with an explicit sim-heuristic rationale.
    assert result.item.content in ("use library X", "use library Y")
    assert result.conflict is not None
    assert result.conflict.policy == "adjudicate"
    assert result.conflict.status == "resolved"
    assert result.conflict.rationale is not None
    assert "[sim heuristic]" in result.conflict.rationale

    # Item actually landed at a new, higher version in the DB.
    assert result.item.version > conflict.current.version

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT content, version FROM memory_items WHERE id = %s",
                (result.item.id,),
            )
            content, version = await cur.fetchone()
    assert content == result.item.content
    assert version == result.item.version

    # A conflict_resolve event was appended (append-only log, never
    # overwritten).
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT op, payload FROM memory_events WHERE item_id = %s AND op = 'conflict_resolve'",
                (result.item.id,),
            )
            rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][1]["policy"] == "adjudicate"


async def test_merge_policy_resolves_and_persists(clean_tables, unique_scope):
    conflict = await _provoke_conflict(unique_scope)

    result = await resolve_conflict(conflict, policy="merge")

    assert "use library X" in result.item.content
    assert "use library Y" in result.item.content
    assert "agent-a" in result.item.content
    assert "agent-b" in result.item.content
    assert result.conflict is not None
    assert result.conflict.policy == "merge"
    assert result.conflict.status == "resolved"
    assert result.item.version > conflict.current.version

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT op FROM memory_events WHERE item_id = %s AND op = 'conflict_resolve'",
                (result.item.id,),
            )
            rows = await cur.fetchall()
    assert len(rows) == 1


async def test_losing_value_is_retained_even_when_not_chosen(
    clean_tables, unique_scope
):
    """adjudicate's sim heuristic discards nothing -- the losing candidate
    (whichever value wasn't chosen as `value`) must still appear verbatim in
    conflicts.resolution, both in the returned ConflictInfo and in the raw
    DB row.
    """
    conflict = await _provoke_conflict(unique_scope)

    result = await resolve_conflict(conflict, policy="adjudicate")

    chosen = result.item.content
    losing = "use library Y" if chosen == "use library X" else "use library X"

    candidate_contents = {
        c["content"] for c in result.conflict.resolution["candidates"]
    }
    assert {"use library X", "use library Y"} == candidate_contents
    assert losing in candidate_contents
    assert result.conflict.resolution["chosen"] == chosen

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT resolution FROM conflicts WHERE item_id = %s",
                (result.item.id,),
            )
            (resolution,) = await cur.fetchone()
    db_candidate_contents = {c["content"] for c in resolution["candidates"]}
    assert {"use library X", "use library Y"} == db_candidate_contents
    assert losing in db_candidate_contents


async def test_resolved_item_version_exceeds_both_input_versions(
    clean_tables, unique_scope
):
    conflict = await _provoke_conflict(unique_scope)

    result = await resolve_conflict(conflict, policy="merge")

    assert result.conflict is not None
    assert result.item.version > result.conflict.version_a
    assert result.item.version > result.conflict.version_b
    assert result.item.version > conflict.current.version
