"""FastAPI app entrypoint -- mounts every domain router and owns the
CockroachDB connection pool's lifecycle via a lifespan context manager.

Route domains (per ARCHITECTURE.md's FastAPI routing convention -- one
router module per domain under app/api/, each exporting `router`, mounted
here with an explicit prefix, no route handlers here beyond the mounts):

  /memory  -- app.api.memory_routes  (U2/U3: write w/ conflict resolution,
              retrieve, direct scope/kind lookup)
  /events  -- app.api.sse_routes     (U4: live SSE change stream)
           +  app.api.event_routes   (U5: time-travel, audit trail, rollback)
              -- both mounted under /events since they're the same "events"
              domain (live stream + historical query) split across two
              modules for the two units that built them.
  /demo    -- app.api.demo_routes    (U6: head-to-head baseline-vs-Quorum race)
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import demo_routes, event_routes, memory_routes, sse_routes
from app.db import close_pool, open_pool

logger = logging.getLogger("quorum")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await open_pool()
    try:
        yield
    finally:
        await close_pool()


app = FastAPI(title="Quorum", lifespan=lifespan)

# Permissive CORS for local dev -- the frontend (Next.js, U7) runs on a
# different port than this API. This is a hackathon demo of a consistency
# mechanism, not a public multi-tenant service (see ARCHITECTURE.md's "What
# NOT to build" -- no auth/multi-tenant system), so wide-open CORS is fine
# here rather than a config surface nobody needs yet.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class UnhandledExceptionCorsMiddleware:
    """Without this, an unhandled exception propagates to Starlette's
    outermost ServerErrorMiddleware, whose fallback 500 response never
    passes through CORSMiddleware's header injection -- the browser then
    blocks it as a cross-origin failure and the frontend sees an opaque
    "TypeError: Failed to fetch" instead of the real error (observed live:
    GET /events/state-at hit a KeyError server-side, and the browser reported
    a bare fetch failure with no indication a 500 had even happened).

    Two things were tried and rejected before this: (1) `@app.exception_handler
    (Exception)` -- Starlette special-cases a bare-Exception handler to run
    inside ServerErrorMiddleware itself, not inside the CORS-wrapped
    ExceptionMiddleware, so it never actually fixes this. (2) `@app.middleware
    ("http")` (BaseHTTPMiddleware) -- confirmed live not to catch exceptions
    raised deep in a route at all (a known Starlette/anyio limitation: the
    downstream app runs in a background task, and exceptions from it don't
    reliably propagate back to a plain try/except around call_next).

    This is instead a raw ASGI middleware with no such limitation -- it
    wraps the app directly and sets the CORS header on its own fallback
    response, matching this app's permissive `allow_origins=["*"]` policy,
    rather than depending on CORSMiddleware to add it.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        except Exception:
            logger.exception(
                "Unhandled exception on %s %s", scope.get("method"), scope.get("path")
            )
            response = JSONResponse(
                status_code=500,
                content={"detail": "Internal server error"},
                headers={"Access-Control-Allow-Origin": "*"},
            )
            await response(scope, receive, send)


app.add_middleware(UnhandledExceptionCorsMiddleware)

app.include_router(memory_routes.router, prefix="/memory", tags=["memory"])
app.include_router(sse_routes.router, prefix="/events", tags=["events"])
app.include_router(event_routes.router, prefix="/events", tags=["events"])
app.include_router(demo_routes.router, prefix="/demo", tags=["demo"])
