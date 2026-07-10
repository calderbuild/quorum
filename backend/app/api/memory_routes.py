"""U2/U3 HTTP surface: write (with conflict resolution), retrieve, and direct
scope/kind lookup.

Mounted by app.main under prefix /memory (per ARCHITECTURE.md's FastAPI
routing convention -- one router per domain, exported as `router`).
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.conflicts.resolver import resolve_conflict
from app.memory.retrieve import retrieve as retrieve_memory
from app.memory.write import ConflictDetected, get_by_scope_kind, remember
from app.models import MemoryItem, WriteResult

router = APIRouter()


class RememberRequest(BaseModel):
    scope: str
    kind: str
    content: str
    agent: str
    policy: Literal["merge", "adjudicate"] = "adjudicate"


def _conflict_to_http(exc: ConflictDetected) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "item_id": str(exc.item_id),
            "current_version": exc.current.version,
            "current_content": exc.current.content,
            "message": str(exc),
        },
    )


@router.post("/remember", response_model=WriteResult)
async def remember_route(body: RememberRequest) -> WriteResult:
    """Write a fact. A detected concurrent write is never silently dropped:
    it is routed through app.conflicts.resolver.resolve_conflict() using the
    requested policy (default "adjudicate"), and the response's `conflict`
    field is populated so the caller can see the race happened -- never a
    blind retry of remember(), per app/memory/write.py's contract.
    """
    try:
        item = await remember(body.scope, body.kind, body.content, body.agent)
        return WriteResult(item=item, conflict=None)
    except ConflictDetected as exc:
        try:
            return await resolve_conflict(exc, policy=body.policy)
        except ConflictDetected as second_exc:
            # A further concurrent write landed while we were resolving the
            # first conflict -- surface it as 409, never silently retry.
            raise _conflict_to_http(second_exc) from second_exc


@router.get("/retrieve", response_model=list[MemoryItem])
async def retrieve_route(
    scope: str = Query(...), query: str = Query(...), k: int = Query(default=5)
) -> list[MemoryItem]:
    """Top-k ANN retrieval within `scope`. See app/memory/retrieve.py's
    docstring: in QUORUM_MODE=sim, ordering is structurally correct but not
    semantically meaningful.
    """
    return await retrieve_memory(scope=scope, query=query, k=k)


@router.get("/{scope}/{kind}", response_model=MemoryItem | None)
async def get_by_scope_kind_route(scope: str, kind: str) -> MemoryItem | None:
    """Direct lookup of the current row for (scope, kind), or null if absent."""
    return await get_by_scope_kind(scope, kind)
