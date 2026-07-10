/**
 * Typed fetch wrapper for the Quorum FastAPI backend.
 *
 * Base URL is configurable via NEXT_PUBLIC_API_URL (defaults to the local
 * dev backend at http://localhost:8000). Types mirror the pydantic models
 * in `backend/app/models.py` -- see ARCHITECTURE.md for the authoritative
 * contract. Route paths below follow the documented prefixes
 * (`/memory`, `/events`, `/demo`) but individual endpoint paths are not yet
 * finalized by the backend units (U2-U6) -- treat `memoryApi`/`eventsApi`/
 * `demoApi` as best-effort helpers and prefer `apiFetch` directly if a path
 * changes.
 */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// ---------------------------------------------------------------------------
// Types (mirror backend/app/models.py)
// ---------------------------------------------------------------------------

export interface MemoryItem {
  id: string;
  scope: string;
  kind: string;
  content: string;
  provenance_agent: string;
  version: number;
  valid: boolean;
  created_at: string;
  updated_at: string;
  distance: number | null;
}

export interface WriteAttempt {
  scope: string;
  kind: string;
  content: string;
  agent: string;
}

export type ConflictPolicy = "merge" | "adjudicate";
export type ConflictStatus = "resolved" | "unresolved";

export interface ConflictInfo {
  conflict_id: string;
  item_id: string;
  version_a: number;
  version_b: number;
  policy: ConflictPolicy;
  resolution: Record<string, unknown>;
  rationale: string | null;
  status: ConflictStatus;
  ts: string;
}

export interface WriteResult {
  item: MemoryItem;
  conflict: ConflictInfo | null;
}

export type ChangeEventOp =
  | "create"
  | "update"
  | "conflict_resolve"
  | "rollback";

export interface ChangeEvent {
  event_id: string;
  item_id: string;
  op: ChangeEventOp;
  scope: string;
  kind: string;
  new_version: number;
  payload: Record<string, unknown>;
  actor_agent: string;
  ts: string;
}

// ---------------------------------------------------------------------------
// Core fetch wrapper
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown, message?: string) {
    super(message ?? `API request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

export interface ApiFetchOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  /** Override the base URL for this call only. */
  baseUrl?: string;
}

/**
 * Thin typed wrapper around fetch: JSON-encodes `body`, JSON-decodes the
 * response, and throws `ApiError` on non-2xx responses.
 */
export async function apiFetch<T>(
  path: string,
  options: ApiFetchOptions = {}
): Promise<T> {
  const { body, baseUrl, headers, ...rest } = options;

  const res = await fetch(`${baseUrl ?? API_BASE_URL}${path}`, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...headers,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  const contentType = res.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await res.json().catch(() => null)
    : await res.text().catch(() => null);

  if (!res.ok) {
    throw new ApiError(res.status, payload);
  }

  return payload as T;
}

// ---------------------------------------------------------------------------
// Endpoint helpers (best-effort; verify against backend/app/api/*_routes.py
// once U2-U6 land, paths below are provisional and may need adjustment)
// ---------------------------------------------------------------------------

export const memoryApi = {
  write: (attempt: WriteAttempt) =>
    apiFetch<WriteResult>("/memory/write", {
      method: "POST",
      body: attempt,
    }),
  get: (scope: string, kind: string) =>
    apiFetch<MemoryItem | null>(
      `/memory/${encodeURIComponent(scope)}/${encodeURIComponent(kind)}`
    ),
  // -- Verified against backend/app/api/memory_routes.py: `scope` is a query
  // param on GET /memory/retrieve, not a path segment -- a path segment here
  // would instead match the /{scope}/{kind} route with kind="retrieve".
  retrieve: (scope: string, query: string, k = 5) =>
    apiFetch<MemoryItem[]>(
      `/memory/retrieve?${new URLSearchParams({ scope, query, k: String(k) }).toString()}`
    ),
};

export const eventsApi = {
  list: (itemId?: string) =>
    apiFetch<ChangeEvent[]>(
      itemId ? `/events?item_id=${encodeURIComponent(itemId)}` : "/events"
    ),
  conflicts: () => apiFetch<ConflictInfo[]>("/events/conflicts"),

  // -- Verified against backend/app/api/event_routes.py (U5) --------------

  /** Raw, chronological (oldest first) memory_events rows for an item. */
  history: (itemId: string) =>
    apiFetch<MemoryEventRow[]>(`/events/history/${encodeURIComponent(itemId)}`),

  /** Reconstruct an item's state as of a timestamp or version (exactly one). */
  stateAt: (
    itemId: string,
    at: { atTs: string } | { atVersion: number }
  ) => {
    const params = new URLSearchParams({ item_id: itemId });
    if ("atTs" in at) params.set("at_ts", at.atTs);
    else params.set("at_version", String(at.atVersion));
    return apiFetch<MemoryItem | null>(`/events/state-at?${params.toString()}`);
  },

  /** "Who changed what when" -- merged audit_log + memory_events rows. */
  auditTrail: (options: { itemId?: string; limit?: number } = {}) => {
    const params = new URLSearchParams();
    if (options.itemId) params.set("item_id", options.itemId);
    params.set("limit", String(options.limit ?? 100));
    return apiFetch<AuditEntry[]>(`/events/audit-trail?${params.toString()}`);
  },

  /** Roll an item back to the content it held at `toVersion`. */
  rollback: (body: {
    item_id: string;
    to_version: number;
    actor_agent: string;
  }) =>
    apiFetch<MemoryItem>("/events/rollback", {
      method: "POST",
      body,
    }),
};

// ---------------------------------------------------------------------------
// Shapes for U5's raw/merged read paths (event_routes.py + timetravel.py +
// audit.py) -- these are dict responses, not the strict pydantic models
// above, so they're typed separately here rather than folded into
// MemoryItem/ChangeEvent.
// ---------------------------------------------------------------------------

/** One row of `memory_events`, as returned verbatim by GET /events/history/{item_id}. */
export interface MemoryEventRow {
  event_id: string;
  item_id: string;
  op: ChangeEventOp;
  prev_version: number | null;
  new_version: number;
  payload: Record<string, unknown>;
  actor_agent: string;
  ts: string;
}

/**
 * One row of GET /events/audit-trail -- a merge of `audit_log` and
 * `memory_events`, normalized by app/events/audit.py into a common shape.
 * `source` distinguishes which table it came from; `prev_version`/
 * `new_version` are only present for `source === "memory_events"`.
 */
export interface AuditEntry {
  entry_id: string;
  actor: string;
  action: string;
  item_id: string | null;
  detail: Record<string, unknown> | null;
  ts: string;
  source: "audit_log" | "memory_events";
  prev_version?: number | null;
  new_version?: number;
}

export const demoApi = {
  headToHead: (scope: string, kind: string, agentCount = 2) =>
    apiFetch<unknown>("/demo/head-to-head", {
      method: "POST",
      body: { scope, kind, agent_count: agentCount },
    }),
};
