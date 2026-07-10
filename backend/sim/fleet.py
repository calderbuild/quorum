"""U6: simulated multi-agent fleet -- the reusable concurrent-write driver
behind the head-to-head demo.

This is the same race `tests/test_write_path.py::
test_concurrent_conflicting_writes_are_detected_not_lost` proves (N agents
issuing concurrent writes to the same (scope, kind) via
`asyncio.gather(..., return_exceptions=True)`), lifted out of the test file
into a reusable driver that `app/demo/head_to_head.py` can point at either
`app.memory.write.remember` (Quorum) or `sim.baseline_lww.lww_write` (the
naive baseline) -- same workload, two different consistency mechanisms.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

WriteFn = Callable[[str, str, str, str], Awaitable[Any]]


async def run_concurrent_writes(
    scope: str, kind: str, facts: list[tuple[str, str]], write_fn: WriteFn
) -> list[Any]:
    """Fire len(facts) concurrent writes to the same (scope, kind) via
    `write_fn`, each write a `(content, agent_name)` pair from `facts`.

    `write_fn` is either `app.memory.write.remember` or
    `sim.baseline_lww.lww_write` -- both share the signature
    `(scope, kind, content, agent) -> Awaitable[Any]`. Uses
    `asyncio.gather(..., return_exceptions=True)` so a raised
    `ConflictDetected` (Quorum side) is captured as a result rather than
    aborting the other concurrent writers, exactly like
    `test_concurrent_conflicting_writes_are_detected_not_lost` -- this
    driver does not itself interpret the results (success vs. exception);
    callers (the demo, or a caller's own tests) do that.
    """
    return await asyncio.gather(
        *(write_fn(scope, kind, content, agent) for content, agent in facts),
        return_exceptions=True,
    )
