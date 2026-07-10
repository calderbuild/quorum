"use client";

/**
 * Quorum console -- the assembled U7 dashboard.
 *
 * Layout, top to bottom:
 *   1. Header (product identity).
 *   2. A single global `scope` selector -- the console's "query bar". It
 *      commits on submit (not per-keystroke) so typing a scope doesn't spam
 *      the backend, and it feeds every scope-aware panel below.
 *   3. HeadToHead -- the primary demo artifact, unscoped (it mints its own
 *      fresh scope per race server-side), kept large and near the top per
 *      ARCHITECTURE.md's U7 contract: "what a judge watches in the first 30
 *      seconds."
 *   4. Live state: MemoryBoard (scoped) + ConflictFeed (global -- it has no
 *      scope prop, conflicts stream across all scopes).
 *   5. History & audit: an item picker (populated by fetching the current
 *      scope's items, same typed `memoryApi.retrieve` the MemoryBoard
 *      component itself uses) feeds TimeTravelScrubber's required `itemId`;
 *      AuditView runs alongside it, global until an item is picked.
 *
 * Natural demo flow: run the race in HeadToHead, copy the scope it prints
 * (`{result.scope} / {result.kind}`) into the scope bar above, watch the
 * item show up in MemoryBoard, then pick it below to scrub its history.
 */

import { type FormEvent, useEffect, useState } from "react";
import { ApiError, type MemoryItem, memoryApi } from "@/lib/api";
import HeadToHead from "@/components/HeadToHead";
import { MemoryBoard } from "@/components/MemoryBoard";
import { ConflictFeed } from "@/components/ConflictFeed";
import { TimeTravelScrubber } from "@/components/TimeTravelScrubber";
import { AuditView } from "@/components/AuditView";

function SectionHeader({
  id,
  title,
  description,
  children,
}: {
  id: string;
  title: string;
  description: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <h2
          id={id}
          className="font-mono text-lg font-medium tracking-tight text-foreground"
        >
          {title}
        </h2>
        <p className="mt-1 max-w-lg text-sm text-foreground-muted">
          {description}
        </p>
      </div>
      {children}
    </div>
  );
}

function EmptyPanel({ label }: { label: string }) {
  return (
    <div className="flex min-h-[140px] flex-col items-center justify-center gap-1 rounded-md border border-dashed border-border bg-background-elevated px-4 py-8 text-center">
      <p className="text-sm text-foreground-muted">{label}</p>
    </div>
  );
}

export default function Home() {
  // Raw text the operator is typing vs. the committed scope actually wired
  // into the panels below -- commits on submit so every keystroke doesn't
  // fire a fetch.
  const [scopeInput, setScopeInput] = useState("");
  const [scope, setScope] = useState("");

  const [items, setItems] = useState<MemoryItem[] | null>(null);
  const [itemsError, setItemsError] = useState<string | null>(null);
  const [itemsLoading, setItemsLoading] = useState(false);
  const [itemId, setItemId] = useState("");
  const [refreshNonce, setRefreshNonce] = useState(0);

  useEffect(() => {
    if (!scope) {
      // Defer to a microtask so this is a callback, not a direct
      // synchronous setState call in the effect body (mirrors lib/sse.ts /
      // ConflictFeed.tsx's established pattern in this codebase).
      queueMicrotask(() => {
        setItems(null);
        setItemsError(null);
        setItemId("");
      });
      return;
    }

    let cancelled = false;
    queueMicrotask(() => {
      if (cancelled) return;
      setItemsLoading(true);
      setItemsError(null);
    });

    memoryApi
      .retrieve(scope, "", 20)
      .then((result) => {
        if (cancelled) return;
        setItems(result);
        setItemsLoading(false);
        // Keep the current selection if it's still present in this scope;
        // otherwise default to the first item so the panels below don't sit
        // empty after a scope switch.
        setItemId((current) =>
          result.some((item) => item.id === current)
            ? current
            : (result[0]?.id ?? "")
        );
      })
      .catch((err) => {
        if (cancelled) return;
        setItems([]);
        setItemsLoading(false);
        setItemsError(
          err instanceof ApiError
            ? `${err.status}: ${err.message}`
            : "Failed to reach memory service"
        );
      });

    return () => {
      cancelled = true;
    };
  }, [scope, refreshNonce]);

  const handleScopeSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setScope(scopeInput.trim());
  };

  return (
    <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-12 px-6 py-12">
      {/* ------------------------------------------------------------- */}
      {/* Header                                                        */}
      {/* ------------------------------------------------------------- */}
      <div className="flex flex-col items-center gap-4 text-center">
        <div className="flex items-center gap-3">
          <span
            className="h-2.5 w-2.5 rounded-full bg-accent shadow-[0_0_8px_var(--accent)]"
            aria-hidden="true"
          />
          <span className="font-mono text-xs uppercase tracking-widest text-foreground-muted">
            quorum_mode: sim
          </span>
        </div>
        <h1 className="font-mono text-4xl font-medium tracking-tight text-foreground sm:text-5xl">
          Quorum
        </h1>
        <p className="max-w-md text-sm leading-6 text-foreground-muted">
          A CockroachDB-backed shared memory layer for multi-agent systems --
          concurrent writes are detected and resolved, never silently lost.
        </p>
      </div>

      {/* ------------------------------------------------------------- */}
      {/* Global scope selector -- the console's query bar               */}
      {/* ------------------------------------------------------------- */}
      <form
        onSubmit={handleScopeSubmit}
        className="flex flex-col gap-3 rounded-md border border-border bg-background-elevated px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
        aria-label="Scope selector"
      >
        <div className="flex flex-1 items-center gap-3">
          <label
            htmlFor="scope-input"
            className="shrink-0 font-mono text-xs uppercase tracking-widest text-foreground-muted"
          >
            scope
          </label>
          <input
            id="scope-input"
            type="text"
            value={scopeInput}
            onChange={(e) => setScopeInput(e.target.value)}
            placeholder="e.g. demo-a1b2c3d4 -- printed inside the race panel below"
            className="w-full min-w-0 flex-1 rounded border border-border bg-background-inset px-2.5 py-1.5 font-mono text-sm text-foreground placeholder:text-foreground-subtle focus:border-accent focus:outline-none"
            spellCheck={false}
            autoComplete="off"
          />
          <button
            type="submit"
            className="shrink-0 rounded border border-border-strong px-3 py-1.5 font-mono text-xs text-foreground-muted transition-colors hover:border-accent hover:text-foreground"
          >
            apply
          </button>
        </div>
        <span className="shrink-0 font-mono text-[11px] text-foreground-subtle">
          {!scope
            ? "no scope selected"
            : itemsLoading
              ? "loading items…"
              : itemsError
                ? "failed to load items"
                : `${items?.length ?? 0} item${items?.length === 1 ? "" : "s"} in scope`}
        </span>
      </form>

      {/* ------------------------------------------------------------- */}
      {/* HeadToHead -- the primary demo artifact                        */}
      {/* ------------------------------------------------------------- */}
      <HeadToHead />

      {/* ------------------------------------------------------------- */}
      {/* Live state: what's stored right now, and conflicts as they land */}
      {/* ------------------------------------------------------------- */}
      <section aria-labelledby="live-state-title">
        <SectionHeader
          id="live-state-title"
          title="Live state"
          description="What's actually stored for the selected scope right now, and every conflict Quorum has resolved across all scopes, live."
        />
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
          <div className="lg:col-span-3">
            {scope ? (
              <MemoryBoard scope={scope} />
            ) : (
              <EmptyPanel label="Enter a scope above to inspect memory state." />
            )}
          </div>
          <div className="lg:col-span-2">
            <ConflictFeed />
          </div>
        </div>
      </section>

      {/* ------------------------------------------------------------- */}
      {/* History & audit: scrub versions, roll back, see who changed what */}
      {/* ------------------------------------------------------------- */}
      <section aria-labelledby="history-audit-title">
        <SectionHeader
          id="history-audit-title"
          title="History & audit"
          description="Step through an item's version history and roll it back, or read who changed what, when."
        >
          <div className="flex items-center gap-2">
            <label
              htmlFor="item-select"
              className="shrink-0 font-mono text-xs uppercase tracking-widest text-foreground-muted"
            >
              item
            </label>
            <select
              id="item-select"
              value={itemId}
              onChange={(e) => setItemId(e.target.value)}
              disabled={!items || items.length === 0}
              className="min-w-0 max-w-xs rounded border border-border bg-background-inset px-2.5 py-1.5 font-mono text-sm text-foreground focus:border-accent focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
            >
              {!scope && <option value="">select a scope first</option>}
              {scope && itemsLoading && <option value="">loading…</option>}
              {scope && !itemsLoading && items && items.length === 0 && (
                <option value="">no items in this scope</option>
              )}
              {items?.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.kind} · v{item.version} ·{" "}
                  {item.content.length > 40
                    ? `${item.content.slice(0, 40)}…`
                    : item.content}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => setRefreshNonce((n) => n + 1)}
              disabled={!scope}
              className="shrink-0 rounded border border-border-strong px-2.5 py-1.5 font-mono text-xs text-foreground-muted transition-colors hover:border-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
            >
              refresh
            </button>
          </div>
        </SectionHeader>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
          <div className="lg:col-span-2">
            {itemId ? (
              <TimeTravelScrubber itemId={itemId} />
            ) : (
              <EmptyPanel
                label={
                  scope
                    ? "Pick an item above to scrub its version history."
                    : "Select a scope, then an item, to enable time travel."
                }
              />
            )}
          </div>
          <div className="lg:col-span-3">
            <AuditView itemId={itemId || undefined} />
          </div>
        </div>
      </section>
    </main>
  );
}
