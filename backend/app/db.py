"""Connection pool + serializable-transaction retry helper.

CockroachDB defaults to SERIALIZABLE isolation. Under contention, a
transaction may be aborted with SQLSTATE 40001 (serialization_failure) and
must be retried by the client -- this is expected, correct behavior, not an
error condition. `run_serializable` centralizes that retry loop so every
write path in the app gets it for free instead of reimplementing it.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import psycopg
from psycopg_pool import AsyncConnectionPool

from app.config import get_settings

T = TypeVar("T")

_pool: AsyncConnectionPool | None = None

SERIALIZATION_FAILURE = "40001"


def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = AsyncConnectionPool(
            conninfo=settings.database_url, open=False, min_size=1, max_size=10
        )
    return _pool


async def open_pool() -> None:
    pool = get_pool()
    if pool.closed:
        await pool.open(wait=True)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def run_serializable(
    fn: Callable[[psycopg.AsyncConnection], Awaitable[T]],
    max_retries: int = 5,
) -> T:
    """Run `fn(conn)` inside a SERIALIZABLE transaction, retrying on 40001.

    `fn` receives an open connection with an active transaction (via
    `async with conn.transaction()` is NOT used here -- callers issue their
    own statements and the transaction is committed on clean return / rolled
    back on exception, per psycopg3 connection.transaction semantics applied
    by the pool context manager below).
    """
    pool = get_pool()
    attempt = 0
    while True:
        attempt += 1
        try:
            async with pool.connection() as conn:
                async with conn.transaction():
                    return await fn(conn)
        except psycopg.errors.SerializationFailure as exc:
            if attempt >= max_retries:
                raise
            backoff = min(0.05 * (2**attempt), 1.0)
            await asyncio.sleep(backoff)
            continue
        except psycopg.OperationalError as exc:
            # CockroachDB sometimes surfaces retryable errors as a generic
            # OperationalError with the 40001 code embedded rather than the
            # specific SerializationFailure subclass; check the sqlstate.
            if (
                getattr(exc, "sqlstate", None) == SERIALIZATION_FAILURE
                and attempt < max_retries
            ):
                backoff = min(0.05 * (2**attempt), 1.0)
                await asyncio.sleep(backoff)
                continue
            raise
