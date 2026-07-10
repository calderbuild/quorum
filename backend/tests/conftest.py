import uuid

import pytest
import pytest_asyncio

from app.db import close_pool, get_pool, open_pool


@pytest_asyncio.fixture(autouse=True)
async def _pool_lifecycle():
    """Function-scoped (not session-scoped) deliberately: psycopg_pool's
    AsyncConnectionPool binds internal asyncio primitives (locks/queues) to
    the event loop active when it opens. pytest-asyncio creates a new event
    loop per test function by default, so a session-scoped pool ends up
    bound to test 1's loop while tests 2+ run on different loops -- the
    pool's own concurrency control then silently misbehaves (observed: a
    genuine two-writer race that should always produce exactly one detected
    conflict instead produced zero, nondeterministically, only inside
    pytest -- an identical standalone asyncio.run() script never reproduced
    it). Recreating the pool per test ties it to that test's own loop.
    """
    await open_pool()
    yield
    await close_pool()


@pytest_asyncio.fixture
async def clean_tables():
    """Truncate the tables each test touches so tests don't interfere."""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            for table in (
                "memory_events",
                "conflicts",
                "audit_log",
                "memory_items",
                "baseline_items",
            ):
                await cur.execute(f"DELETE FROM {table}")
        await conn.commit()
    yield


@pytest.fixture
def unique_scope() -> str:
    """A fresh scope string per test, so tests never collide on (scope, kind)."""
    return f"test-scope-{uuid.uuid4().hex[:8]}"
