"""U2: conflict resolution -- reconciles a ConflictDetected into a durable,
non-lossy resolution.

`resolve_conflict()` is the ONLY intended handler for
`app.memory.write.ConflictDetected`. It never retries `remember()` blindly
(that would silently overwrite the other agent's committed fact -- exactly
the bug class this project exists to prevent). Instead it:

1. Decides a resolved value via the given policy (`merge` or `adjudicate`).
2. Writes that value as a NEW version of the item, using the same
   optimistic-CAS pattern `app.memory.write._remember_txn` uses (CAS against
   `conflict.current.version`, raise `ConflictDetected` again on a miss
   rather than silently overwriting a third concurrent write).
3. Persists a `conflicts` row capturing both candidate values (winner and
   loser) plus the chosen resolution and rationale.
4. Appends a `conflict_resolve` event to `memory_events`.

The losing candidate is ALWAYS retained in `conflicts.resolution.candidates`
even when it is not the chosen value -- resolution never silently drops a
fact, it only decides which one becomes the new current value.

Merge policy (deterministic, no LLM, fully offline): concatenate both
values, each tagged with its provenance agent, e.g.
    "[merged from agent-a] use library X | [merged from agent-b] use library Y"
This is a defensible default for a facts-as-text memory store: unlike
last-write-wins it never discards information, and unlike `adjudicate` it
requires no LLM call, so it stays fully deterministic and QUORUM_MODE=sim
compatible. It intentionally does not attempt semantic reconciliation --
callers who want a single coherent synthesized value should use
policy="adjudicate" instead, which delegates that judgment to
`app.llm.adjudicate.adjudicate` (sim-mode: longer/more-specific heuristic).
"""

from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from app.db import get_pool
from app.llm.adjudicate import adjudicate
from app.llm.embeddings import embed, vector_literal
from app.memory.write import ConflictDetected, _row_to_item
from app.models import ConflictInfo, MemoryItem, WriteAttempt, WriteResult


def _merge_value(attempt: WriteAttempt, current: MemoryItem) -> str:
    return (
        f"[merged from {attempt.agent}] {attempt.content} | "
        f"[merged from {current.provenance_agent}] {current.content}"
    )


async def resolve_conflict(
    conflict: ConflictDetected, policy: Literal["merge", "adjudicate"]
) -> WriteResult:
    attempt = conflict.attempt
    current = conflict.current

    if policy == "adjudicate":
        result = await adjudicate(
            attempt.content, current.content, context=f"{attempt.scope}/{attempt.kind}"
        )
        resolved_value = result.value
        rationale = result.rationale
    elif policy == "merge":
        resolved_value = _merge_value(attempt, current)
        rationale = (
            "[merge] Deterministic concatenation of both concurrent values, "
            "tagged by provenance agent -- no information discarded."
        )
    else:
        raise ValueError(f"Unknown policy: {policy!r}")

    # Both candidates always ride along in the resolution JSONB, regardless
    # of which one (if either, verbatim) was chosen -- the losing value must
    # never be silently dropped.
    resolution: dict[str, Any] = {
        "chosen": resolved_value,
        "candidates": [
            {"content": attempt.content, "agent": attempt.agent, "source": "attempt"},
            {
                "content": current.content,
                "agent": current.provenance_agent,
                "source": "current",
            },
        ],
    }

    vec = vector_literal(await embed(resolved_value))
    pool = get_pool()
    async with pool.connection() as conn, conn.transaction():
        item, conflict_info = await _resolve_txn(
            conn, conflict, policy, resolved_value, vec, resolution, rationale
        )
    return WriteResult(item=item, conflict=conflict_info)


async def _resolve_txn(
    conn: psycopg.AsyncConnection,
    conflict: ConflictDetected,
    policy: str,
    resolved_value: str,
    vec: str,
    resolution: dict[str, Any],
    rationale: str,
) -> tuple[MemoryItem, ConflictInfo]:
    current = conflict.current

    async with conn.cursor(row_factory=dict_row) as cur:
        # Same CAS pattern as _remember_txn's UPDATE branch: write the
        # resolved value as a new version, guarded against a third
        # concurrent write landing between conflict detection and here.
        await cur.execute(
            """
                UPDATE memory_items
                SET content = %s, embedding = %s, provenance_agent = %s,
                    version = version + 1, updated_at = now()
                WHERE id = %s AND version = %s
                RETURNING *
                """,
            (
                resolved_value,
                vec,
                f"conflict-resolver:{policy}",
                current.id,
                current.version,
            ),
        )
        row = await cur.fetchone()

        if row is None:
            # CAS miss: yet another write landed while we were resolving.
            # Never overwrite silently -- surface it as a fresh conflict for
            # the caller to resolve again.
            await cur.execute("SELECT * FROM memory_items WHERE id = %s", (current.id,))
            fresh = await cur.fetchone()
            raise ConflictDetected(current.id, conflict.attempt, _row_to_item(fresh))

        item = _row_to_item(row)

        # Both candidate versions raced for the same next version number
        # (current.version is the version that landed and won the CAS race
        # that produced this conflict; the loser targeted that identical
        # version and never committed one of its own) -- record it as both
        # version_a and version_b. The full candidate contents/agents live
        # in `resolution`, so no information is lost by this simplification.
        await cur.execute(
            """
                INSERT INTO conflicts (item_id, version_a, version_b, policy, resolution, rationale, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'resolved')
                RETURNING *
                """,
            (
                conflict.item_id,
                current.version,
                current.version,
                policy,
                Json(resolution),
                rationale,
            ),
        )
        conflict_row = await cur.fetchone()

        await cur.execute(
            """
                INSERT INTO memory_events (item_id, op, prev_version, new_version, payload, actor_agent)
                VALUES (%s, 'conflict_resolve', %s, %s, %s, %s)
                """,
            (
                item.id,
                current.version,
                item.version,
                Json(
                    {
                        "policy": policy,
                        # `content` is the universal key every memory_events
                        # payload carries (see app/events/timetravel.py's
                        # module docstring) -- app.events.timetravel.get_state_at
                        # replays this key regardless of op type. `resolved_content`
                        # is kept as an alias for readability at this call site
                        # and because ConflictFeed.tsx / test_demo.py already
                        # read it directly.
                        "content": resolved_value,
                        "resolved_content": resolved_value,
                        "rationale": rationale,
                        "candidates": resolution["candidates"],
                    }
                ),
                f"conflict-resolver:{policy}",
            ),
        )

        conflict_info = ConflictInfo(
            conflict_id=conflict_row["conflict_id"],
            item_id=conflict_row["item_id"],
            version_a=conflict_row["version_a"],
            version_b=conflict_row["version_b"],
            policy=conflict_row["policy"],
            resolution=conflict_row["resolution"],
            rationale=conflict_row["rationale"],
            status=conflict_row["status"],
            ts=conflict_row["ts"],
        )
        return item, conflict_info
