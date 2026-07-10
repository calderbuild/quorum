"""U3: semantic retrieval structural-correctness tests.

QUORUM_MODE=sim embeddings are deterministic hash-based pseudo-vectors, NOT a
real semantic model -- near-duplicate text is not guaranteed to embed close
together. These tests therefore assert structural correctness only (scoping,
distance population, ordering, k, empty-result behavior), never semantic
relevance.
"""

import pytest

from app.memory.retrieve import retrieve
from app.memory.write import remember

pytestmark = pytest.mark.asyncio


async def test_retrieve_only_returns_items_from_the_given_scope(
    clean_tables, unique_scope
):
    other_scope = f"{unique_scope}-other"
    await remember(unique_scope, "fact", "in-scope fact one", "agent-a")
    await remember(unique_scope, "fact2", "in-scope fact two", "agent-a")
    await remember(other_scope, "fact", "out-of-scope fact", "agent-b")

    results = await retrieve(unique_scope, "some query", k=10)

    assert len(results) == 2
    assert all(item.scope == unique_scope for item in results)


async def test_retrieve_populates_distance(clean_tables, unique_scope):
    await remember(unique_scope, "fact", "a fact to retrieve", "agent-a")

    results = await retrieve(unique_scope, "some query", k=5)

    assert len(results) == 1
    assert results[0].distance is not None
    assert isinstance(results[0].distance, float)


async def test_retrieve_orders_by_ascending_distance(clean_tables, unique_scope):
    for i in range(5):
        await remember(unique_scope, f"fact-{i}", f"content number {i}", "agent-a")

    results = await retrieve(unique_scope, "query text", k=5)

    distances = [item.distance for item in results]
    assert distances == sorted(distances)


async def test_retrieve_respects_k(clean_tables, unique_scope):
    for i in range(5):
        await remember(unique_scope, f"fact-{i}", f"content number {i}", "agent-a")

    results = await retrieve(unique_scope, "query text", k=3)

    assert len(results) == 3


async def test_retrieve_on_empty_scope_returns_empty_list(clean_tables, unique_scope):
    results = await retrieve(unique_scope, "no facts exist here", k=5)

    assert results == []
