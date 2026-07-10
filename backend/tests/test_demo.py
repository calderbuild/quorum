"""U6: the head-to-head demo -- the single most important correctness proof
in the whole project. Runs the identical concurrent-write race against the
naive baseline and against Quorum, live against the cluster, and asserts on
the exact difference in outcome: the baseline genuinely loses one fact with
no trace, Quorum detects exactly one conflict and both facts remain
recoverable.
"""

import pytest

from app.demo.head_to_head import run_head_to_head
from sim.baseline_lww import lww_read

pytestmark = pytest.mark.asyncio


async def test_head_to_head_baseline_loses_fact_quorum_preserves_both(
    clean_tables, unique_scope
):
    result = await run_head_to_head(
        scope=unique_scope,
        kind="policy",
        seed_content="initial value",
        fact_a="use library X",
        fact_b="use library Y",
    )

    baseline = result["baseline"]
    quorum = result["quorum"]

    # --- Baseline side: a genuine, silent lost update. ---
    assert baseline["conflict_detected"] is False
    assert baseline["final_value"] in ("use library X", "use library Y")
    assert baseline["lost_value"] in ("use library X", "use library Y")
    assert baseline["final_value"] != baseline["lost_value"]

    # No trace of the lost value survives anywhere in the baseline's own
    # data -- re-reading it independently must still show only the winner.
    reread = await lww_read(unique_scope, "policy")
    assert reread == baseline["final_value"]
    assert reread != baseline["lost_value"]

    # --- Quorum side: exactly one conflict detected, nothing lost. ---
    assert quorum["conflict_detected"] is True
    assert quorum["conflict"] is not None
    assert quorum["conflict"]["policy"] == "merge"
    assert quorum["conflict"]["status"] == "resolved"

    # Both original facts are recoverable: one directly named in the
    # resolution's candidate list...
    candidate_contents = {
        c["content"] for c in quorum["conflict"]["resolution"]["candidates"]
    }
    assert candidate_contents == {"use library X", "use library Y"}

    # ...and merge policy's resolved value concatenates both, so the
    # current row itself also carries both facts.
    assert "use library X" in quorum["final_value"]
    assert "use library Y" in quorum["final_value"]

    # The full event history (create -> conflict_resolve) is present and
    # non-destructive: the seed value's 'content' key is still visible in the
    # 'create' event's payload (write.py's convention), and a
    # 'conflict_resolve' event was appended carrying both candidates
    # (resolver.py's convention -- its payload key is 'resolved_content',
    # not 'content').
    create_events = [ev for ev in quorum["history"] if ev["op"] == "create"]
    assert len(create_events) == 1
    assert create_events[0]["payload"]["content"] == "initial value"

    resolve_events = [ev for ev in quorum["history"] if ev["op"] == "conflict_resolve"]
    assert len(resolve_events) == 1
    resolve_candidates = {
        c["content"] for c in resolve_events[0]["payload"]["candidates"]
    }
    assert resolve_candidates == {"use library X", "use library Y"}


async def test_head_to_head_is_repeatable_with_fresh_scopes(clean_tables, unique_scope):
    """Running the demo twice with different scopes must not interfere with
    each other -- each race is scoped and independent.
    """
    scope_2 = f"{unique_scope}-2"

    result_1 = await run_head_to_head(
        scope=unique_scope,
        kind="policy",
        seed_content="initial value",
        fact_a="use library X",
        fact_b="use library Y",
    )
    result_2 = await run_head_to_head(
        scope=scope_2,
        kind="policy",
        seed_content="initial value",
        fact_a="use library A",
        fact_b="use library B",
    )

    assert result_1["quorum"]["conflict_detected"] is True
    assert result_2["quorum"]["conflict_detected"] is True
    assert "use library A" in result_2["quorum"]["final_value"] or (
        "use library B" in result_2["quorum"]["final_value"]
    )
    # Scope 1's candidates must not have leaked scope 2's facts or vice versa.
    candidates_1 = {
        c["content"] for c in result_1["quorum"]["conflict"]["resolution"]["candidates"]
    }
    candidates_2 = {
        c["content"] for c in result_2["quorum"]["conflict"]["resolution"]["candidates"]
    }
    assert candidates_1 == {"use library X", "use library Y"}
    assert candidates_2 == {"use library A", "use library B"}
