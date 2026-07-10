"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { API_BASE_URL, ApiError, apiFetch } from "@/lib/api";
import { useSSE } from "@/lib/sse";

export interface ConflictFeedProps {
  /** How many resolved conflicts to keep in the feed. Default 50. */
  maxEntries?: number;
  /** Polling interval (ms) used only when the SSE stream is unreachable. */
  pollIntervalMs?: number;
  className?: string;
}

// ---------------------------------------------------------------------------
// Shapes on the wire
// ---------------------------------------------------------------------------

/** Raw event from GET /events/stream (app/changefeed/consumer.py). */
interface ChangefeedEvent {
  table: string;
  key: unknown;
  after: Record<string, unknown> | null;
  updated: string | null;
}

/** `after` payload shape for the `conflicts` table (app/models.py ConflictInfo,
 * plus resolution.candidates from app/conflicts/resolver.py). */
interface ConflictsRow {
  conflict_id: string;
  item_id: string;
  policy: "merge" | "adjudicate";
  resolution: {
    chosen: string;
    candidates: { content: string; agent: string; source: string }[];
  };
  rationale: string | null;
  status: "resolved" | "unresolved";
  ts: string;
}

/** Fallback shape: a `conflict_resolve` entry from GET /events/audit-trail. */
interface AuditTrailEntry {
  entry_id: string;
  item_id: string;
  action: string;
  actor: string;
  detail: Record<string, unknown>;
  ts: string;
  source: string;
}

// ---------------------------------------------------------------------------
// Normalized shape the UI renders
// ---------------------------------------------------------------------------

interface ConflictEntry {
  id: string;
  itemId: string;
  policy: "merge" | "adjudicate" | "unknown";
  rationale: string | null;
  chosen: string;
  candidates: { content: string; agent: string }[];
  ts: string;
}

function fromConflictsRow(row: ConflictsRow): ConflictEntry {
  return {
    id: row.conflict_id,
    itemId: row.item_id,
    policy: row.policy,
    rationale: row.rationale,
    chosen: row.resolution?.chosen ?? "",
    candidates: row.resolution?.candidates ?? [],
    ts: row.ts,
  };
}

function fromAuditEntry(entry: AuditTrailEntry): ConflictEntry {
  const detail = entry.detail ?? {};
  return {
    id: entry.entry_id,
    itemId: entry.item_id,
    policy: (detail.policy as "merge" | "adjudicate") ?? "unknown",
    rationale: (detail.rationale as string) ?? null,
    chosen: (detail.resolved_content as string) ?? "",
    candidates:
      (detail.candidates as { content: string; agent: string }[]) ?? [],
    ts: entry.ts,
  };
}

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

/**
 * ConflictFeed -- live-updating feed of resolved concurrent-write conflicts.
 *
 * Primary transport: SSE at `/events/stream` (`event: change`), filtered to
 * rows on the `conflicts` table (see app/changefeed/consumer.py's
 * WATCHED_TABLES). Each row already carries both candidate values, the
 * chosen/winning value, and the rationale (app/conflicts/resolver.py never
 * drops the losing candidate).
 *
 * Fallback: if the stream doesn't open within a few seconds (e.g. dev
 * environment without changefeed/rangefeed support), falls back to polling
 * `GET /events/audit-trail` and filtering for `conflict_resolve` entries,
 * which carry the same candidate/rationale payload in `detail`.
 */
export function ConflictFeed({
  maxEntries = 50,
  pollIntervalMs = 4000,
  className,
}: ConflictFeedProps) {
  const [entries, setEntries] = useState<ConflictEntry[]>([]);
  const [fallbackActive, setFallbackActive] = useState(false);
  const [fallbackError, setFallbackError] = useState<string | null>(null);
  const seenIds = useRef<Set<string>>(new Set());
  const fallbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const { status, events } = useSSE<ChangefeedEvent>("/events/stream", {
    eventName: "change",
    enabled: !fallbackActive,
    maxEvents: 200,
  });

  const upsert = (incoming: ConflictEntry[]) => {
    if (incoming.length === 0) return;
    setEntries((prev) => {
      const merged = [...prev];
      for (const entry of incoming) {
        if (seenIds.current.has(entry.id)) continue;
        seenIds.current.add(entry.id);
        merged.unshift(entry);
      }
      merged.sort((a, b) => (a.ts < b.ts ? 1 : -1));
      return merged.slice(0, maxEntries);
    });
  };

  // Consume SSE events as they arrive, keeping only `conflicts` table rows.
  useEffect(() => {
    const conflictRows = events
      .filter((event) => event.table === "conflicts" && event.after !== null)
      .map((event) => fromConflictsRow(event.after as unknown as ConflictsRow));
    upsert(conflictRows);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events]);

  // If the SSE connection hasn't opened within 5s, or errors out, switch to
  // polling the audit trail instead -- e.g. local dev without changefeed
  // support enabled on the cluster.
  useEffect(() => {
    if (fallbackActive) return;

    if (status === "error") {
      // Defer to a microtask so this is a callback, not a direct
      // synchronous setState call in the effect body (mirrors lib/sse.ts).
      queueMicrotask(() => setFallbackActive(true));
      return;
    }

    if (status === "connecting" || status === "idle") {
      fallbackTimerRef.current = setTimeout(() => {
        setFallbackActive(true);
      }, 5000);
    } else if (status === "open" && fallbackTimerRef.current) {
      clearTimeout(fallbackTimerRef.current);
      fallbackTimerRef.current = null;
    }

    return () => {
      if (fallbackTimerRef.current) clearTimeout(fallbackTimerRef.current);
    };
  }, [status, fallbackActive]);

  // Polling fallback.
  useEffect(() => {
    if (!fallbackActive) return;

    let cancelled = false;

    const poll = async () => {
      try {
        const rows = await apiFetch<AuditTrailEntry[]>(
          `/events/audit-trail?limit=${maxEntries}`,
          { baseUrl: API_BASE_URL }
        );
        if (cancelled) return;
        const conflictEntries = rows
          .filter((r) => r.action === "conflict_resolve")
          .map(fromAuditEntry);
        upsert(conflictEntries);
        setFallbackError(null);
      } catch (err) {
        if (cancelled) return;
        setFallbackError(
          err instanceof ApiError
            ? `${err.status}: ${err.message}`
            : "Failed to reach events service"
        );
      }
    };

    poll();
    const timer = setInterval(poll, pollIntervalMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fallbackActive, maxEntries, pollIntervalMs]);

  const liveLabel = useMemo(() => {
    if (fallbackActive) return { text: "polling", ok: false };
    if (status === "open") return { text: "live", ok: true };
    if (status === "connecting" || status === "idle")
      return { text: "connecting", ok: false };
    return { text: "reconnecting", ok: false };
  }, [status, fallbackActive]);

  return (
    <section
      className={`flex flex-col gap-3 rounded-md border border-border bg-background-elevated ${className ?? ""}`}
    >
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              liveLabel.ok
                ? "bg-status-ok shadow-[0_0_6px_var(--status-ok)]"
                : "bg-status-conflict shadow-[0_0_6px_var(--status-conflict)]"
            }`}
            aria-hidden="true"
          />
          <h2 className="font-mono text-xs uppercase tracking-widest text-foreground-muted">
            conflict_feed
          </h2>
        </div>
        <span className="font-mono text-[10px] uppercase tracking-wider text-foreground-subtle">
          {liveLabel.text}
        </span>
      </header>

      <div className="flex flex-col gap-2 px-4 pb-4">
        {fallbackError && (
          <div
            role="alert"
            className="rounded border border-status-error/40 bg-status-error/10 px-3 py-2 text-sm text-status-error"
          >
            {fallbackError}
          </div>
        )}

        {entries.length === 0 && !fallbackError && (
          <div className="flex flex-col items-center gap-1 py-8 text-center">
            <p className="text-sm text-foreground-muted">
              No conflicts detected yet.
            </p>
            <p className="font-mono text-xs text-foreground-subtle">
              waiting for concurrent writes to the same fact
            </p>
          </div>
        )}

        <ul className="flex flex-col gap-2">
          {entries.map((entry) => (
            <li
              key={entry.id}
              className="rounded border border-border bg-background-inset p-3"
            >
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <span className="rounded border border-status-conflict/50 bg-status-conflict/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-status-conflict">
                    {entry.policy}
                  </span>
                  <span className="font-mono text-[11px] text-foreground-subtle">
                    item {entry.itemId.slice(0, 8)}
                  </span>
                </div>
                <span className="font-mono text-[10px] text-foreground-subtle">
                  {formatTimestamp(entry.ts)}
                </span>
              </div>

              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {entry.candidates.map((candidate, idx) => {
                  const won = candidate.content === entry.chosen;
                  return (
                    <div
                      key={`${entry.id}-${idx}`}
                      className={`rounded border p-2 ${
                        won
                          ? "border-accent/60 bg-accent/10"
                          : "border-border-strong bg-background-elevated"
                      }`}
                    >
                      <div className="mb-1 flex items-center justify-between gap-2">
                        <span className="font-mono text-[10px] text-data-cyan">
                          {candidate.agent}
                        </span>
                        {won && (
                          <span className="rounded bg-accent px-1 py-0.5 font-mono text-[9px] font-medium uppercase tracking-wider text-background">
                            won
                          </span>
                        )}
                      </div>
                      <p className="break-words text-xs text-foreground">
                        {candidate.content}
                      </p>
                    </div>
                  );
                })}
              </div>

              {!entry.candidates.some((c) => c.content === entry.chosen) &&
                entry.chosen && (
                  <div className="mt-2 rounded border border-accent/60 bg-accent/10 p-2">
                    <span className="mb-1 block font-mono text-[10px] uppercase tracking-wider text-accent-strong">
                      resolved
                    </span>
                    <p className="break-words text-xs text-foreground">
                      {entry.chosen}
                    </p>
                  </div>
                )}

              {entry.rationale && (
                <p className="mt-2 border-t border-border pt-2 text-xs leading-5 text-foreground-muted">
                  {entry.rationale}
                </p>
              )}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

export default ConflictFeed;
