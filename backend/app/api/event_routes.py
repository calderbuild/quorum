"""U5 HTTP surface: time-travel + audit-trail queries, and rollback.

Mounted by app.main under prefix /events (per ARCHITECTURE.md's FastAPI
routing convention -- one router per domain, exported as `router`).
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.events.audit import get_audit_trail
from app.events.timetravel import get_history, get_state_at, rollback
from app.memory.write import ConflictDetected
from app.models import MemoryItem

router = APIRouter()


class RollbackRequest(BaseModel):
    item_id: UUID
    to_version: int
    actor_agent: str


class ConflictResponse(BaseModel):
    item_id: UUID
    current_version: int
    current_content: str
    detail: str


@router.get("/history/{item_id}")
async def history(item_id: UUID) -> list[dict]:
    """Raw, chronological memory_events rows for an item."""
    return await get_history(item_id)


@router.get("/state-at", response_model=MemoryItem | None)
async def state_at(
    item_id: UUID,
    at_ts: datetime | None = Query(default=None),
    at_version: int | None = Query(default=None),
) -> MemoryItem | None:
    """Reconstruct an item's state as of a timestamp or version.

    Exactly one of at_ts/at_version must be given.
    """
    if (at_ts is None) == (at_version is None):
        raise HTTPException(
            status_code=400, detail="exactly one of at_ts or at_version must be given"
        )
    return await get_state_at(item_id, at_ts=at_ts, at_version=at_version)


@router.get("/audit-trail")
async def audit_trail(
    item_id: UUID | None = Query(default=None), limit: int = Query(default=100)
) -> list[dict]:
    return await get_audit_trail(item_id=item_id, limit=limit)


@router.post("/rollback", response_model=MemoryItem)
async def rollback_route(body: RollbackRequest) -> MemoryItem:
    try:
        return await rollback(body.item_id, body.to_version, body.actor_agent)
    except ConflictDetected as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "item_id": str(exc.item_id),
                "current_version": exc.current.version,
                "current_content": exc.current.content,
                "message": str(exc),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
