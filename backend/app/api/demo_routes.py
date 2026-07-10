"""U6 HTTP surface: the head-to-head demo endpoint.

Mounted by app.main under prefix /demo (per ARCHITECTURE.md's FastAPI
routing convention -- one router per domain, exported as `router`).
"""

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.demo.head_to_head import run_head_to_head

router = APIRouter()


class HeadToHeadRequest(BaseModel):
    scope: str | None = None
    kind: str = "policy"
    seed_content: str = "initial value"
    fact_a: str = "use library X"
    fact_b: str = "use library Y"


@router.post("/head-to-head")
async def head_to_head(req: HeadToHeadRequest) -> dict[str, Any]:
    # A fresh scope per call by default so repeated demo runs from the
    # frontend never collide with each other's (scope, kind) key.
    scope = req.scope or f"demo-{uuid.uuid4().hex[:8]}"
    return await run_head_to_head(
        scope=scope,
        kind=req.kind,
        seed_content=req.seed_content,
        fact_a=req.fact_a,
        fact_b=req.fact_b,
    )
