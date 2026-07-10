"""U3: semantic retrieval over `memory_items` via CockroachDB's vector index.

Embeds the query text and runs an ANN (`<->` distance operator) search
against `memory_items_embedding_idx`, scoped to a single `scope` so agents
never see another scope's facts. Read-only and naturally idempotent, so it
goes through `run_serializable` (unlike `write.py`'s `remember()`, which
deliberately does NOT use it -- see that module's docstring).

In QUORUM_MODE=sim, embeddings are deterministic hash-based pseudo-vectors,
NOT a real semantic model -- near-duplicate text does not embed close
together. Retrieval here is structurally correct (right scope, right
ordering, right count) but callers should not assume semantic relevance in
sim mode.
"""

import psycopg
from psycopg.rows import dict_row

from app.db import run_serializable
from app.llm.embeddings import embed, vector_literal
from app.models import MemoryItem


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
        distance=row["distance"],
    )


async def retrieve(scope: str, query: str, k: int = 5) -> list[MemoryItem]:
    """Return the top-k memory items in `scope` nearest to `query` by ANN distance."""
    vec = vector_literal(await embed(query))

    async def _txn(conn: psycopg.AsyncConnection) -> list[MemoryItem]:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                    SELECT *, embedding <-> %s AS distance
                    FROM memory_items
                    WHERE scope = %s
                    ORDER BY embedding <-> %s
                    LIMIT %s
                    """,
                (vec, scope, vec, k),
            )
            rows = await cur.fetchall()
        return [_row_to_item(row) for row in rows]

    return await run_serializable(_txn)
