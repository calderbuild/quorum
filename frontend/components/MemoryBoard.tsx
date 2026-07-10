"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, memoryApi, type MemoryItem } from "@/lib/api";

export interface MemoryBoardProps {
  /** Memory scope to display, e.g. "demo-abc123". */
  scope: string;
  /**
   * Query used for the retrieve() ANN search. In QUORUM_MODE=sim this only
   * affects ordering, not which rows come back within top-k -- see
   * lib/api.ts / backend/app/memory/retrieve.py. Empty string is a valid
   * "give me whatever's in this scope" query.
   */
  query?: string;
  k?: number;
  /** Poll interval in ms; 0 disables polling (fetch once). Default 4000. */
  pollIntervalMs?: number;
  className?: string;
}

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

/**
 * MemoryBoard -- current memory_items state for a scope, as a dense
 * console-style table. Polls `GET /memory/retrieve` on an interval since
 * there is no dedicated "list all items in scope" route; retrieve() with an
 * empty query returns top-k rows for the scope, ordered by ANN distance to
 * "" (structurally stable in QUORUM_MODE=sim -- see retrieve.py docstring).
 */
export function MemoryBoard({
  scope,
  query = "",
  k = 20,
  pollIntervalMs = 4000,
  className,
}: MemoryBoardProps) {
  const [items, setItems] = useState<MemoryItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastFetchedAt, setLastFetchedAt] = useState<Date | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await memoryApi.retrieve(scope, query, k);
      setItems(result);
      setError(null);
      setLastFetchedAt(new Date());
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `${err.status}: ${err.message}`
          : "Failed to reach memory service"
      );
    }
  }, [scope, query, k]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | undefined;

    const run = async () => {
      if (cancelled) return;
      await load();
    };

    run();
    if (pollIntervalMs > 0) {
      timer = setInterval(run, pollIntervalMs);
    }

    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [load, pollIntervalMs]);

  return (
    <section
      className={`flex flex-col gap-3 rounded-md border border-border bg-background-elevated ${className ?? ""}`}
    >
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <span
            className="h-1.5 w-1.5 rounded-full bg-accent shadow-[0_0_6px_var(--accent)]"
            aria-hidden="true"
          />
          <h2 className="font-mono text-xs uppercase tracking-widest text-foreground-muted">
            memory_board
          </h2>
          <span className="rounded border border-border-strong px-1.5 py-0.5 font-mono text-[10px] text-foreground-subtle">
            {scope}
          </span>
        </div>
        {lastFetchedAt && (
          <span className="font-mono text-[10px] text-foreground-subtle">
            synced {formatTimestamp(lastFetchedAt.toISOString())}
          </span>
        )}
      </header>

      <div className="px-4 pb-4">
        {error && (
          <div
            role="alert"
            className="rounded border border-status-error/40 bg-status-error/10 px-3 py-2 text-sm text-status-error"
          >
            {error}
          </div>
        )}

        {!error && items === null && (
          <ul className="flex flex-col gap-1.5" aria-busy="true">
            {[0, 1, 2].map((i) => (
              <li
                key={i}
                className="h-9 animate-pulse rounded bg-background-inset"
              />
            ))}
          </ul>
        )}

        {!error && items !== null && items.length === 0 && (
          <div className="flex flex-col items-center gap-1 py-8 text-center">
            <p className="text-sm text-foreground-muted">
              No memory items in this scope yet.
            </p>
            <p className="font-mono text-xs text-foreground-subtle">
              waiting for the first remember() write
            </p>
          </div>
        )}

        {!error && items !== null && items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] border-collapse text-left text-sm">
              <thead>
                <tr className="border-b border-border text-foreground-subtle">
                  <th className="py-2 pr-3 font-mono text-[10px] font-normal uppercase tracking-wider">
                    kind
                  </th>
                  <th className="py-2 pr-3 font-mono text-[10px] font-normal uppercase tracking-wider">
                    content
                  </th>
                  <th className="py-2 pr-3 font-mono text-[10px] font-normal uppercase tracking-wider">
                    version
                  </th>
                  <th className="py-2 pr-3 font-mono text-[10px] font-normal uppercase tracking-wider">
                    agent
                  </th>
                  <th className="py-2 pr-0 font-mono text-[10px] font-normal uppercase tracking-wider">
                    updated
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr
                    key={item.id}
                    className="border-b border-border/60 last:border-b-0"
                  >
                    <td className="py-2 pr-3 align-top">
                      <span className="rounded border border-border-strong px-1.5 py-0.5 font-mono text-[11px] text-foreground-muted">
                        {item.kind}
                      </span>
                    </td>
                    <td className="max-w-xs py-2 pr-3 align-top text-foreground">
                      <span className="line-clamp-2 break-words">
                        {item.content}
                      </span>
                    </td>
                    <td className="py-2 pr-3 align-top font-mono text-foreground-muted">
                      v{item.version}
                    </td>
                    <td className="py-2 pr-3 align-top font-mono text-data-cyan">
                      {item.provenance_agent}
                    </td>
                    <td className="py-2 pr-0 align-top font-mono text-[11px] text-foreground-subtle">
                      {formatTimestamp(item.updated_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

export default MemoryBoard;
