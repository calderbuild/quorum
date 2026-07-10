"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, eventsApi, type AuditEntry } from "@/lib/api";

export interface AuditViewProps {
  /** Restrict the trail to one item's history; omit for the global trail. */
  itemId?: string;
  limit?: number;
  /** Poll for new entries every N ms (0/undefined disables polling). */
  pollIntervalMs?: number;
}

type LoadState = "loading" | "ready" | "error";

function formatTs(ts: string): string {
  try {
    return new Date(ts).toLocaleString(undefined, {
      dateStyle: "medium",
      timeStyle: "medium",
    });
  } catch {
    return ts;
  }
}

function actionColor(action: string): string {
  switch (action) {
    case "rollback":
      return "text-status-conflict";
    case "conflict_resolve":
      return "text-status-conflict";
    case "create":
      return "text-status-ok";
    default:
      return "text-data-cyan";
  }
}

/**
 * Chronological "who changed what when" table over GET /events/audit-trail
 * (a merge of `audit_log` and `memory_events`, per app/events/audit.py).
 */
export function AuditView({ itemId, limit = 100, pollIntervalMs }: AuditViewProps) {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState("loading");
    try {
      const rows = await eventsApi.auditTrail({ itemId, limit });
      setEntries(rows);
      setState("ready");
      setError(null);
    } catch (err) {
      setState("error");
      setError(
        err instanceof ApiError
          ? `${err.status}: ${JSON.stringify(err.body)}`
          : String(err)
      );
    }
  }, [itemId, limit]);

  useEffect(() => {
    const run = async () => {
      await load();
    };
    void run();
    if (!pollIntervalMs) return;
    const id = setInterval(() => void load(), pollIntervalMs);
    return () => clearInterval(id);
  }, [load, pollIntervalMs]);

  return (
    <div className="rounded-md border border-border bg-background-elevated">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h3 className="font-mono text-xs uppercase tracking-widest text-foreground-muted">
          audit trail{itemId ? ` · ${itemId.slice(0, 8)}` : ""}
        </h3>
        <button
          type="button"
          onClick={() => void load()}
          className="rounded border border-border-strong px-2 py-1 font-mono text-[11px] text-foreground-muted transition-colors hover:border-foreground-muted hover:text-foreground"
        >
          refresh
        </button>
      </div>

      {state === "loading" && (
        <p className="px-4 py-6 font-mono text-xs text-foreground-subtle">
          loading audit trail&hellip;
        </p>
      )}

      {state === "error" && (
        <p className="px-4 py-6 font-mono text-xs text-status-error">
          failed to load audit trail: {error}
        </p>
      )}

      {state === "ready" && entries.length === 0 && (
        <p className="px-4 py-6 font-mono text-xs text-foreground-subtle">
          no audit entries{itemId ? " for this item" : ""} yet.
        </p>
      )}

      {state === "ready" && entries.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left font-mono text-xs">
            <thead>
              <tr className="border-b border-border text-foreground-subtle">
                <th className="px-4 py-2 font-normal uppercase tracking-wider">timestamp</th>
                <th className="px-4 py-2 font-normal uppercase tracking-wider">actor</th>
                <th className="px-4 py-2 font-normal uppercase tracking-wider">action</th>
                <th className="px-4 py-2 font-normal uppercase tracking-wider">item</th>
                <th className="px-4 py-2 font-normal uppercase tracking-wider">detail</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr
                  key={entry.entry_id}
                  className="border-b border-border/60 last:border-0 hover:bg-background-inset"
                >
                  <td className="whitespace-nowrap px-4 py-2 text-foreground-muted">
                    {formatTs(entry.ts)}
                  </td>
                  <td className="px-4 py-2 text-foreground">{entry.actor}</td>
                  <td className={`px-4 py-2 ${actionColor(entry.action)}`}>
                    {entry.action}
                    {entry.new_version !== undefined ? ` → v${entry.new_version}` : ""}
                  </td>
                  <td className="px-4 py-2 text-foreground-muted">
                    {entry.item_id ? entry.item_id.slice(0, 8) : "—"}
                  </td>
                  <td className="max-w-xs truncate px-4 py-2 text-foreground-subtle">
                    {entry.detail ? JSON.stringify(entry.detail) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default AuditView;
