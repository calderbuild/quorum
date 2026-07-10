"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, eventsApi, type MemoryEventRow, type MemoryItem } from "@/lib/api";

export interface TimeTravelScrubberProps {
  /** UUID of the memory_items row to scrub through. */
  itemId: string;
  /** Agent name to record as the actor if a rollback is performed. */
  actorAgent?: string;
  /** Called after a successful rollback with the item's new (post-rollback) state. */
  onRolledBack?: (item: MemoryItem) => void;
}

type LoadState = "idle" | "loading" | "ready" | "error";

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

/**
 * Slider over an item's version history: drag to a version, see the
 * historical content Quorum reconstructed for it (via GET
 * /events/state-at), and optionally roll the live item back to that
 * version (POST /events/rollback).
 */
export function TimeTravelScrubber({
  itemId,
  actorAgent = "console-operator",
  onRolledBack,
}: TimeTravelScrubberProps) {
  const [history, setHistory] = useState<MemoryEventRow[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("idle");
  const [loadError, setLoadError] = useState<string | null>(null);

  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [snapshot, setSnapshot] = useState<MemoryItem | null>(null);
  const [snapshotState, setSnapshotState] = useState<LoadState>("idle");
  const [snapshotError, setSnapshotError] = useState<string | null>(null);

  const [rollbackState, setRollbackState] = useState<
    "idle" | "confirming" | "rolling-back" | "done" | "error"
  >("idle");
  const [rollbackError, setRollbackError] = useState<string | null>(null);

  const versions = useMemo(
    () => history.map((ev) => ev.new_version).sort((a, b) => a - b),
    [history]
  );

  const loadHistory = useCallback(async () => {
    setLoadState("loading");
    setLoadError(null);
    setSnapshot(null);
    try {
      const rows = await eventsApi.history(itemId);
      setHistory(rows);
      setLoadState("ready");
      if (rows.length > 0) {
        const latest = rows[rows.length - 1].new_version;
        setSelectedVersion(latest);
      }
    } catch (err) {
      setLoadState("error");
      setLoadError(
        err instanceof ApiError
          ? `${err.status}: ${JSON.stringify(err.body)}`
          : String(err)
      );
    }
  }, [itemId]);

  useEffect(() => {
    const reset = async () => {
      setHistory([]);
      setSelectedVersion(null);
      setRollbackState("idle");
      await loadHistory();
    };
    void reset();
  }, [loadHistory]);

  useEffect(() => {
    if (selectedVersion === null) return;
    let cancelled = false;

    const loadSnapshot = async () => {
      setSnapshotState("loading");
      setSnapshotError(null);
      try {
        const item = await eventsApi.stateAt(itemId, { atVersion: selectedVersion });
        if (cancelled) return;
        setSnapshot(item);
        setSnapshotState("ready");
      } catch (err) {
        if (cancelled) return;
        setSnapshotState("error");
        setSnapshotError(
          err instanceof ApiError
            ? `${err.status}: ${JSON.stringify(err.body)}`
            : String(err)
        );
      }
    };

    void loadSnapshot();
    return () => {
      cancelled = true;
    };
  }, [itemId, selectedVersion]);

  const selectedEvent = useMemo(
    () => history.find((ev) => ev.new_version === selectedVersion) ?? null,
    [history, selectedVersion]
  );

  const isLatestSelected =
    versions.length > 0 && selectedVersion === versions[versions.length - 1];

  const handleRollback = useCallback(async () => {
    if (selectedVersion === null) return;
    setRollbackState("rolling-back");
    setRollbackError(null);
    try {
      const item = await eventsApi.rollback({
        item_id: itemId,
        to_version: selectedVersion,
        actor_agent: actorAgent,
      });
      setRollbackState("done");
      onRolledBack?.(item);
      await loadHistory();
    } catch (err) {
      setRollbackState("error");
      setRollbackError(
        err instanceof ApiError
          ? `${err.status}: ${JSON.stringify(err.body)}`
          : String(err)
      );
    }
  }, [actorAgent, itemId, loadHistory, onRolledBack, selectedVersion]);

  if (loadState === "loading" || loadState === "idle") {
    return (
      <div className="rounded-md border border-border bg-background-elevated p-4">
        <p className="font-mono text-xs text-foreground-subtle">
          loading version history&hellip;
        </p>
      </div>
    );
  }

  if (loadState === "error") {
    return (
      <div className="rounded-md border border-status-error/40 bg-background-elevated p-4">
        <p className="font-mono text-xs text-status-error">
          failed to load history: {loadError}
        </p>
        <button
          type="button"
          onClick={() => void loadHistory()}
          className="mt-2 rounded border border-border-strong px-2 py-1 font-mono text-xs text-foreground-muted transition-colors hover:border-foreground-muted hover:text-foreground"
        >
          retry
        </button>
      </div>
    );
  }

  if (versions.length === 0) {
    return (
      <div className="rounded-md border border-border bg-background-elevated p-4">
        <p className="font-mono text-xs text-foreground-subtle">
          no history for this item yet.
        </p>
      </div>
    );
  }

  const minVersion = versions[0];
  const maxVersion = versions[versions.length - 1];

  return (
    <div className="rounded-md border border-border bg-background-elevated p-4">
      <div className="flex items-center justify-between gap-4">
        <h3 className="font-mono text-xs uppercase tracking-widest text-foreground-muted">
          time travel
        </h3>
        <span className="font-mono text-xs text-foreground-subtle">
          v{minVersion} &ndash; v{maxVersion}
        </span>
      </div>

      <div className="mt-4 flex items-center gap-3">
        <span className="font-mono text-xs text-foreground-subtle">v{minVersion}</span>
        <input
          type="range"
          min={minVersion}
          max={maxVersion}
          step={1}
          value={selectedVersion ?? maxVersion}
          onChange={(e) => setSelectedVersion(Number(e.target.value))}
          className="h-1.5 flex-1 cursor-pointer appearance-none rounded-full bg-border-strong accent-accent"
          aria-label="Select a version to inspect"
        />
        <span className="font-mono text-xs text-foreground-subtle">v{maxVersion}</span>
      </div>

      <div className="mt-2 flex items-center justify-between">
        <span className="font-mono text-sm text-accent">
          v{selectedVersion ?? "?"}
          {isLatestSelected ? " (current)" : ""}
        </span>
        {selectedEvent && (
          <span className="font-mono text-xs text-foreground-subtle">
            {selectedEvent.op} &middot; {selectedEvent.actor_agent} &middot;{" "}
            {formatTs(selectedEvent.ts)}
          </span>
        )}
      </div>

      <div className="mt-4 rounded border border-border bg-background-inset p-3">
        {snapshotState === "loading" && (
          <p className="font-mono text-xs text-foreground-subtle">
            reconstructing state&hellip;
          </p>
        )}
        {snapshotState === "error" && (
          <p className="font-mono text-xs text-status-error">{snapshotError}</p>
        )}
        {snapshotState === "ready" &&
          (snapshot ? (
            <div className="space-y-1">
              <p className="whitespace-pre-wrap break-words font-mono text-sm text-foreground">
                {snapshot.content}
              </p>
              <p className="font-mono text-xs text-foreground-subtle">
                provenance: {snapshot.provenance_agent} &middot; updated{" "}
                {formatTs(snapshot.updated_at)}
              </p>
            </div>
          ) : (
            <p className="font-mono text-xs text-foreground-subtle">
              no reconstructed state at this version.
            </p>
          ))}
      </div>

      <div className="mt-4 flex items-center justify-end gap-3">
        {rollbackState === "error" && (
          <span className="font-mono text-xs text-status-error">{rollbackError}</span>
        )}
        {rollbackState === "done" && (
          <span className="font-mono text-xs text-status-ok">rolled back.</span>
        )}
        {rollbackState === "confirming" ? (
          <>
            <span className="font-mono text-xs text-foreground-muted">
              roll live item back to v{selectedVersion}?
            </span>
            <button
              type="button"
              onClick={() => setRollbackState("idle")}
              className="rounded border border-border-strong px-3 py-1.5 font-mono text-xs text-foreground-muted transition-colors hover:border-foreground-muted hover:text-foreground"
            >
              cancel
            </button>
            <button
              type="button"
              onClick={() => void handleRollback()}
              className="rounded border border-status-error bg-status-error/10 px-3 py-1.5 font-mono text-xs text-status-error transition-colors hover:bg-status-error/20"
            >
              confirm rollback
            </button>
          </>
        ) : (
          <button
            type="button"
            disabled={
              isLatestSelected || selectedVersion === null || rollbackState === "rolling-back"
            }
            onClick={() => setRollbackState("confirming")}
            className="rounded border border-accent-dim px-3 py-1.5 font-mono text-xs text-accent transition-colors hover:border-accent hover:text-accent-strong disabled:cursor-not-allowed disabled:opacity-40"
          >
            {rollbackState === "rolling-back" ? "rolling back…" : "roll back to this version"}
          </button>
        )}
      </div>
    </div>
  );
}

export default TimeTravelScrubber;
