"""U4: prove the changefeed consumer sees a real write land, live.

`clean_tables` truncates memory_items/memory_events/conflicts before this
test runs, so the changefeed's initial scan (which would otherwise replay
every pre-existing row) starts from empty tables -- the only event we should
observe is the one `remember()` call this test makes.
"""

import asyncio

import pytest

from app.changefeed.consumer import changefeed_events
from app.memory.write import remember

pytestmark = pytest.mark.asyncio


async def _write_after_delay(scope: str, delay: float = 0.5) -> None:
    # Give the changefeed a moment to finish its (empty) initial scan and
    # start blocking on new changes before we write, so we don't race the
    # feed's own startup.
    await asyncio.sleep(delay)
    await remember(scope, "fact", "hello from changefeed test", "agent-cf")


async def test_write_produces_visible_changefeed_event(clean_tables, unique_scope):
    write_task = asyncio.create_task(_write_after_delay(unique_scope))

    gen = changefeed_events(("memory_items",))
    found = None
    try:
        async with asyncio.timeout(15):
            async for event in gen:
                if event["table"] == "memory_items" and (
                    event["after"] is not None
                    and event["after"].get("scope") == unique_scope
                ):
                    found = event
                    break
    finally:
        await gen.aclose()
        await write_task

    assert found is not None, "changefeed never surfaced our write within 15s"
    assert found["after"]["content"] == "hello from changefeed test"
    assert found["after"]["kind"] == "fact"
    assert found["after"]["provenance_agent"] == "agent-cf"
    assert found["key"] is not None


async def test_conflict_resolve_event_visible_on_memory_events(
    clean_tables, unique_scope
):
    """A second table (memory_events) also streams live -- not just memory_items."""
    await remember(unique_scope, "fact", "v1", "agent-a")

    async def _update_after_delay() -> None:
        await asyncio.sleep(0.5)
        await remember(unique_scope, "fact", "v2", "agent-b")

    update_task = asyncio.create_task(_update_after_delay())

    gen = changefeed_events(("memory_events",))
    found = None
    try:
        async with asyncio.timeout(15):
            async for event in gen:
                after = event["after"]
                if (
                    event["table"] == "memory_events"
                    and after is not None
                    and after.get("op") == "update"
                    and after.get("actor_agent") == "agent-b"
                ):
                    found = event
                    break
    finally:
        await gen.aclose()
        await update_task

    assert found is not None, (
        "changefeed never surfaced the update event on memory_events"
    )
    assert found["after"]["new_version"] == 2
