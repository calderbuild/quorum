"""U4: SSE endpoint fanning out live changefeed events to the frontend.

Mounted (per ARCHITECTURE.md's FastAPI routing convention) at whatever
prefix `app/main.py` chooses -- this module only defines the router and its
relative paths.
"""

import json

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.changefeed.consumer import changefeed_events

router = APIRouter()


@router.get("/stream")
async def stream() -> EventSourceResponse:
    """Live SSE stream of changes to memory_items/memory_events/conflicts.

    Each SSE message's `data` is a JSON-encoded dict shaped like
    `changefeed_events()`'s yield: {"table", "key", "after", "updated"}.
    """

    async def event_generator():
        async for event in changefeed_events():
            yield {"event": "change", "data": json.dumps(event, default=str)}

    return EventSourceResponse(event_generator())
