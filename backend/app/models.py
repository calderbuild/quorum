from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel


class MemoryItem(BaseModel):
    id: UUID
    scope: str
    kind: str
    content: str
    provenance_agent: str
    version: int
    valid: bool
    created_at: datetime
    updated_at: datetime
    distance: float | None = None  # populated on retrieval (ANN distance), else None


class WriteAttempt(BaseModel):
    scope: str
    kind: str
    content: str
    agent: str


class ConflictInfo(BaseModel):
    conflict_id: UUID
    item_id: UUID
    version_a: int
    version_b: int
    policy: Literal["merge", "adjudicate"]
    resolution: dict[str, Any]
    rationale: str | None
    status: Literal["resolved", "unresolved"]
    ts: datetime


class WriteResult(BaseModel):
    item: MemoryItem
    conflict: ConflictInfo | None = None


class ChangeEvent(BaseModel):
    event_id: UUID
    item_id: UUID
    op: Literal["create", "update", "conflict_resolve", "rollback"]
    scope: str
    kind: str
    new_version: int
    payload: dict[str, Any]
    actor_agent: str
    ts: datetime
