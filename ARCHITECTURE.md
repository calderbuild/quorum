# Quorum -- Architecture Contract

This document is the binding interface contract for everyone building on top of
U1 (the write-path consistency core, already implemented and test-verified
against a live 3-node CockroachDB cluster). Read this before writing any code.
Do not rename, re-signature, or re-architect anything listed under "Already
built -- do not change" without a very good reason; downstream units depend on
these exact names and shapes.

## Thesis (why this project exists)

Multiple AI agents sharing memory (Mem0/Zep/Letta-style single-agent recall
stores, or a bare vector DB) have no answer for concurrent writes: agent A and
agent B both read the current fact, both decide to write a new one, and
whichever write lands last silently wins -- the other vanishes with no error,
no trace, no way to know a conflict ever happened. `backend/sim/baseline_lww.py`
is a faithful, uncharitable implementation of that exact failure mode, proven
by `test_baseline_lww_silently_loses_a_concurrent_write`.

Quorum's answer: every write goes through CockroachDB SERIALIZABLE isolation
with an explicit optimistic version CAS (`UPDATE ... WHERE version = $N`). A
concurrent write is never silently overwritten -- it is detected as a
`ConflictDetected` and routed to policy-driven resolution, with full event
history and time-travel. This is proven live, not asserted: run the same
concurrent workload against `baseline_lww.py` (loses data) and against
`app/memory/write.py::remember()` (detects and preserves both values) --
that side-by-side IS the head-to-head demo (U6).

## Repo layout (current + planned)

```
quorum/
  docker-compose.yml          # 3-node CockroachDB, verified working
  infra/crdb/init.sql         # schema, verified live against the cluster
  .env.template
  backend/
    requirements.txt
    app/
      config.py               # Settings -- DONE
      models.py                # pydantic models -- DONE
      db.py                    # pool + run_serializable -- DONE
      memory/
        write.py               # remember(), get_by_scope_kind() -- DONE
        retrieve.py             # U3 -- TODO
      llm/
        embeddings.py           # embed(), vector_literal() -- DONE
        adjudicate.py            # adjudicate() -- DONE
      conflicts/
        resolver.py              # U2 -- TODO
      changefeed/
        consumer.py               # U4 -- TODO
      events/
        timetravel.py              # U5 -- TODO
        audit.py                   # U5 -- TODO
      demo/
        head_to_head.py             # U6 -- TODO
      mcp/
        server.py                    # U6 -- TODO
      api/
        sse_routes.py               # U4 -- TODO
        event_routes.py             # U5 -- TODO
        demo_routes.py               # U6 -- TODO
        memory_routes.py             # U2/U3 wiring -- TODO
      main.py                         # integration -- TODO
    sim/
      baseline_lww.py           # DONE
      fleet.py                   # U6 -- TODO (simulated multi-agent fleet)
    tests/
      conftest.py                # DONE -- reuse these fixtures, do not duplicate
      test_write_path.py          # DONE, 6/6 passing
      test_conflicts.py            # U2
      test_retrieve.py             # U3
      test_changefeed.py            # U4
      test_timetravel.py             # U5
      test_demo.py                    # U6
  frontend/                       # U7 -- TODO, Next.js 15 + Tailwind
```

## Already built -- do not change (U1, verified)

### `app/config.py`

```python
class Settings(BaseSettings):
    quorum_mode: Literal["sim", "openai", "bedrock"] = "sim"
    database_url: str = "postgresql://root@localhost:26257/quorum?sslmode=disable"
    embedding_dim: int = 128
    openai_api_key: str | None
    aws_region: str = "us-east-1"
    aws_access_key_id: str | None
    aws_secret_access_key: str | None
    audit_s3_bucket: str | None

def get_settings() -> Settings  # @lru_cache singleton
```

Every unit reads config via `get_settings()`. Never read env vars directly.
`QUORUM_MODE=sim` is the default and MUST remain fully offline/deterministic
-- all tests and the primary demo path run in sim mode with zero API keys.

### `app/models.py` (pydantic, all fields already final)

```python
class MemoryItem(BaseModel):
    id: UUID; scope: str; kind: str; content: str; provenance_agent: str
    version: int; valid: bool; created_at: datetime; updated_at: datetime
    distance: float | None = None  # populated by retrieve.py's ANN query, else None

class WriteAttempt(BaseModel):
    scope: str; kind: str; content: str; agent: str

class ConflictInfo(BaseModel):
    conflict_id: UUID; item_id: UUID; version_a: int; version_b: int
    policy: Literal["merge", "adjudicate"]; resolution: dict[str, Any]
    rationale: str | None; status: Literal["resolved", "unresolved"]; ts: datetime

class WriteResult(BaseModel):
    item: MemoryItem; conflict: ConflictInfo | None = None

class ChangeEvent(BaseModel):
    event_id: UUID; item_id: UUID
    op: Literal["create", "update", "conflict_resolve", "rollback"]
    scope: str; kind: str; new_version: int; payload: dict[str, Any]
    actor_agent: str; ts: datetime
```

`ConflictInfo` and `ChangeEvent` are the shapes U2/U4/U5 must produce -- do not
invent parallel/competing shapes.

### `app/db.py`

```python
def get_pool() -> AsyncConnectionPool          # singleton, min_size=1 max_size=10
async def open_pool() -> None
async def close_pool() -> None
async def run_serializable(fn: Callable[[AsyncConnection], Awaitable[T]], max_retries=5) -> T
```

`run_serializable` retries on SQLSTATE 40001 -- use it for READ-ONLY or
naturally-idempotent operations only (`get_by_scope_kind`, `retrieve`,
`timetravel` queries, audit reads). **Never** wrap a write that must detect
conflicts in `run_serializable` -- see the warning in `app/memory/write.py`'s
docstring for exactly why (auto-retry silently converts a real conflict into
a silent overwrite; this was a live-caught bug, not theoretical).

### `app/memory/write.py` -- the load-bearing wall

```python
class ConflictDetected(Exception):
    item_id: UUID; attempt: WriteAttempt; current: MemoryItem

async def remember(scope: str, kind: str, content: str, agent: str) -> MemoryItem
    # Raises ConflictDetected if a concurrent write landed first.
    # Idempotent: identical (scope, kind, content) re-sent is a no-op, same version.
    # Manages its own connection/transaction -- does NOT use run_serializable.

async def get_by_scope_kind(scope: str, kind: str) -> MemoryItem | None
```

U2's `resolve_conflict()` is the ONLY intended caller of `ConflictDetected` as
a control-flow signal: catch it, decide the resolved value (merge or
adjudicate), then perform its own follow-up write + `conflicts` row + a
`conflict_resolve` event. Do not add a "just retry `remember()`" path anywhere
in the codebase -- that is precisely the bug class this project exists to
prevent, and it already bit us once during U1 development.

### `app/llm/embeddings.py`

```python
async def embed(text: str) -> list[float]        # dispatches on settings.quorum_mode
def vector_literal(vec: list[float]) -> str        # '[0.1,0.2,...]' for SQL
```

sim mode: SHA256-seeded deterministic PRNG unit vector (NOT semantically
meaningful -- near-duplicate text does not embed close together). Fine for
correctness tests; retrieval demos in sim mode should not claim semantic
relevance, only structural correctness (right row, right dimension, ANN query
executes and returns ordered-by-distance results).

### `app/llm/adjudicate.py`

```python
class AdjudicationResult(BaseModel):
    value: str; rationale: str

async def adjudicate(fact_a: str, fact_b: str, context: str) -> AdjudicationResult
```

sim mode: heuristic (longer/more-specific value wins), rationale explicitly
prefixed `[sim heuristic]`. U2 calls this when `policy == "adjudicate"`.

### `sim/baseline_lww.py`

```python
async def lww_write(scope: str, kind: str, content: str, agent: str) -> None
async def lww_read(scope: str, kind: str) -> str | None
async def reset_baseline() -> None
```

Writes to the separate `baseline_items` table (no version column, no CAS).
This is the "before" side of the head-to-head demo -- never wire it into the
real `/memory` API routes, it exists only for the contrast demo and its own
test.

### `infra/crdb/init.sql` (schema, live-verified)

Tables: `memory_items` (scope, kind, content, `embedding VECTOR(128)`,
provenance_agent, version, valid, timestamps, `UNIQUE(scope,kind)`, vector
index `memory_items_embedding_idx`), `memory_events` (append-only, indexed on
`(item_id, ts)`), `conflicts` (indexed on `(item_id, ts)`), `audit_log`
(indexed on `ts`), `agents`, `baseline_items` (the naive comparison table).
Do not add migrations framework for the hackathon timeline -- edit `init.sql`
directly and re-apply via `docker cp` + `cockroach sql -f` (see git history /
ask if the exact command is needed); the cluster is disposable local dev
infra, not a production system requiring migration discipline.

### `docker-compose.yml`

3-node CockroachDB (`crdb1`/`crdb2`/`crdb3`) + `crdb-init` one-shot service.
Healthcheck is deliberately liveness-only (`/health`, not `/health?ready=1`)
to avoid a circular dependency with `crdb-init` -- do not "fix" this back to
`ready=1`, it was already tried and deadlocks.

## Conventions every new file must follow

**Async everywhere.** All DB-touching functions are `async def` using
psycopg3's `AsyncConnectionPool` via `get_pool()`. No sync psycopg, no
sync FastAPI routes for anything that touches the DB.

**Module-level functions, not classes.** The codebase so far has zero classes
except pydantic models and the one `ConflictDetected` exception. Keep new
modules as plain async functions grouped by file, matching the existing style
-- don't introduce a service-object/DI-container pattern.

**Errors are exceptions, not sentinel returns.** `ConflictDetected` is raised,
not returned as `Optional[...]`. Follow the same pattern for any new
domain-specific failure (e.g. if U5's rollback hits an already-rolled-back
target, raise, don't return `None` and hope the caller checks).

**Sim-mode-first.** Every external dependency (LLM calls, embeddings,
adjudication) MUST have a working, fully offline `sim` branch dispatched via
`settings.quorum_mode`, exactly like `embeddings.py` and `adjudicate.py`
already do. The entire test suite and the primary demo path run with zero API
keys. `openai`/`bedrock` branches should exist and be plausible but are not
required to be hand-verified against live APIs given the timeline -- do not
block on obtaining real API keys.

**Testing conventions (see `tests/conftest.py`, do not duplicate):**
- `_pool_lifecycle` (autouse, function-scoped -- NOT session-scoped, see its
  docstring for why: psycopg_pool binds to the event loop active at open time,
  and pytest-asyncio's default per-function event loop breaks a
  session-scoped pool's internal concurrency control) already opens/closes
  the pool per test. New test files should NOT create their own pool
  fixtures.
- `clean_tables` (function fixture) truncates all six tables. Depend on it
  in any test that writes to the DB.
- `unique_scope` (function fixture) gives a fresh `scope` string so tests
  never collide on `(scope, kind)`.
- `pytestmark = pytest.mark.asyncio` at module level, per existing files.
- Concurrency-correctness tests (anything proving conflict detection) MUST
  use `asyncio.gather(..., return_exceptions=True)` and assert on the exact
  count of successes vs. `ConflictDetected` instances, exactly like
  `test_concurrent_conflicting_writes_are_detected_not_lost` does. Do not
  write a concurrency test that only checks the final DB state -- that hides
  exactly the class of bug U1 caught during development (auto-retry
  silently converting a conflict into a clean-looking overwrite).
- Run tests with:
  ```bash
  cd backend && source .venv/bin/activate
  export DATABASE_URL="postgresql://root@localhost:26257/quorum?sslmode=disable"
  export QUORUM_MODE=sim
  PYTHONPATH=. python -m pytest tests/ -v
  ```
  The cluster must already be up (`docker compose up -d` from repo root).

**FastAPI routing.** One router module per domain under `app/api/`
(`memory_routes.py`, `sse_routes.py`, `event_routes.py`, `demo_routes.py`),
each exporting an `APIRouter` named `router`, mounted in `app/main.py` with an
explicit prefix (`/memory`, `/events`, `/demo`). Do not put route handlers
directly in `main.py` beyond the mount calls and app-lifespan pool
open/close.

## Unit contracts for the remaining work

### U2 -- `app/conflicts/resolver.py`

```python
async def resolve_conflict(conflict: ConflictDetected, policy: Literal["merge","adjudicate"]) -> WriteResult
```
Catches what `remember()` raises. `policy="adjudicate"` calls
`app.llm.adjudicate.adjudicate(conflict.attempt.content, conflict.current.content, context=f"{conflict.attempt.scope}/{conflict.attempt.kind}")`
and writes the resolved value as a new version (via the same CAS pattern
`_remember_txn` uses -- reuse or closely mirror it, don't duplicate SQL by
hand). `policy="merge"` is a simpler deterministic strategy your choice (e.g.
concatenate both facts with provenance tags) -- pick something defensible and
document the rationale field. Every resolution writes: (1) a `conflicts` row
capturing both versions + policy + resolution + rationale + status=`resolved`,
(2) a `conflict_resolve` event in `memory_events`. Never silently drop the
losing value -- it must appear in the `conflicts.resolution` JSONB even if not
chosen.

### U3 -- `app/memory/retrieve.py`

```python
async def retrieve(scope: str, query: str, k: int = 5) -> list[MemoryItem]
```
Embeds `query` via `app.llm.embeddings.embed`, runs an ANN query using the
`<->` operator against `memory_items_embedding_idx`, scoped to `scope`,
`ORDER BY embedding <-> %s LIMIT k`, populates `MemoryItem.distance`. Use
`run_serializable` (read-only, safe to retry).

### U4 -- `app/changefeed/consumer.py` + `app/api/sse_routes.py`

Consume `CREATE CHANGEFEED FOR memory_items, memory_events, conflicts` (core
sinkless changefeed, already confirmed working against this cluster) and
fan events out over Server-Sent Events (`sse-starlette`) so a frontend can
watch writes/conflicts land live. If wiring the changefeed cursor into the app
process proves awkward under the timeline, a documented polling fallback
(short-interval `SELECT ... WHERE ts > $last_seen`) is acceptable -- note it
explicitly in the module docstring as a fallback, don't silently ship a poll
loop pretending to be a changefeed consumer.

### U5 -- `app/events/timetravel.py` + `app/events/audit.py` + `app/api/event_routes.py`

Time-travel is via the **event log**, not CockroachDB's native
`AS OF SYSTEM TIME` (which is GC-TTL-bounded and unsuitable for unlimited
history) -- replay `memory_events` rows up to a given `ts` or `version` to
reconstruct historical state, and support rollback by writing a new `rollback`
event (never delete/mutate history). `audit.py` provides a queryable read
path over `audit_log` for "who changed what when."

### U6 -- `app/mcp/server.py`, `sim/fleet.py`, `app/demo/head_to_head.py`, `app/api/demo_routes.py`

`fleet.py` simulates N agents issuing concurrent writes (uses `remember()` and
`lww_write()` in parallel against the same `(scope, kind)`, same pattern as
`test_concurrent_conflicting_writes_are_detected_not_lost` but reusable as a
demo driver, not just a test). `head_to_head.py` runs the identical workload
against both `remember()` and `lww_write()`/`baseline_lww` and returns a
structured comparison (what each side has after the race: Quorum shows both
values + a resolved conflict + full history; baseline shows one value, no
trace of the other). `demo_routes.py` exposes this over HTTP for the frontend.
MCP server exposes `remember`/`retrieve`/`get_conflicts` as MCP tools per the
Python MCP SDK.

### U7 -- `frontend/` (Next.js 15, Tailwind, TypeScript)

Dark technical theme -- explicitly NOT the generic AI-purple-gradient
aesthetic. This is a systems/infra tool, closer to a CockroachDB Console /
Grafana visual register than a consumer SaaS landing page. `lib/api.ts` (fetch
wrapper), `lib/sse.ts` (EventSource wrapper for U4's stream). Components:
`MemoryBoard` (current state per scope), `ConflictFeed` (live conflict
stream), `TimeTravelScrubber` + `AuditView` (U5), and **`HeadToHead.tsx`** --
the single most important artifact in the whole demo: a split-screen showing
the identical concurrent-write race running against the naive baseline (left,
visibly loses a fact) and Quorum (right, visibly catches and resolves the
conflict) side by side, live, on trigger. This component is what a judge
watches in the first 30 seconds; it must be unambiguous without narration.

## What NOT to build (scope discipline)

- No auth/multi-tenant user system -- this is a hackathon demo of a
  consistency mechanism, not a SaaS product.
- No migrations framework, no ORM -- raw SQL via psycopg3 throughout, matches
  `write.py`'s existing style.
- No Kubernetes/Terraform for the local dev path -- Docker Compose only. AWS
  deployment (U8) is a separate, explicitly-checkpointed, human-approved step
  outside this build (real billing implications) -- do not provision AWS
  resources as part of implementing U2-U7.
- No new conflict-resolution policies beyond `merge`/`adjudicate` unless a
  real gap is found -- resist the urge to add a policy enum value "just in
  case."
