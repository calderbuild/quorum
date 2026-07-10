"use client";

/**
 * HeadToHead -- the single most important visual artifact in the project.
 *
 * Fires the real POST /demo/head-to-head race (see
 * backend/app/api/demo_routes.py + backend/app/demo/head_to_head.py) and
 * choreographs the identical result as a synchronized split-screen replay:
 * the naive last-write-wins baseline (left) silently loses a fact, Quorum
 * (right) catches the same collision, flags it, and keeps both values
 * inspectable. One `phase` state drives both columns from the same clock so
 * they visibly run "at the same time" -- they only diverge at the moment of
 * impact, which is the entire point.
 *
 * The backend runs both races synchronously and returns the finished
 * result in a single response (there is no live socket for this specific
 * race) -- MIN_RACE_MS/IMPACT_HOLD_MS below turn that single response into a
 * legible few-second playback instead of an instant, illegible jump-cut.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiFetch } from "@/lib/api";

// ---------------------------------------------------------------------------
// Types -- mirror backend/app/demo/head_to_head.py's actual return shape
// (verified against demo_routes.py, resolver.py, timetravel.py, init.sql).
// Kept local/precise rather than reusing lib/api.ts's ConflictInfo/ChangeEvent,
// whose `resolution`/`payload` fields are intentionally generic there.
// ---------------------------------------------------------------------------

interface ConflictCandidate {
  content: string;
  agent: string;
  source: "attempt" | "current";
}

interface ConflictResolution {
  chosen: string;
  candidates: ConflictCandidate[];
}

interface DemoConflictInfo {
  conflict_id: string;
  item_id: string;
  version_a: number;
  version_b: number;
  policy: "merge" | "adjudicate";
  resolution: ConflictResolution;
  rationale: string | null;
  status: "resolved" | "unresolved";
  ts: string;
}

interface MemoryEventRow {
  event_id: string;
  item_id: string;
  op: "create" | "update" | "conflict_resolve" | "rollback";
  prev_version: number | null;
  new_version: number;
  payload: { content: string; [key: string]: unknown };
  actor_agent: string;
  ts: string;
}

interface HeadToHeadResult {
  scope: string;
  kind: string;
  seed_content: string;
  fact_a: string;
  fact_b: string;
  baseline: {
    final_value: string;
    lost_value: string;
    conflict_detected: false;
  };
  quorum: {
    final_value: string;
    conflict: DemoConflictInfo | null;
    history: MemoryEventRow[];
    conflict_detected: true;
  };
}

type Phase = "idle" | "racing" | "impact" | "resolved";

// The scenario: two agents write conflicting facts to the same shared-memory
// key at the same time. `scope` is intentionally omitted so the backend
// mints a fresh one per run (see demo_routes.py) -- repeat runs never collide.
const RACE_PAYLOAD = {
  kind: "user_preference",
  seed_content: "(unset)",
  fact_a: "user prefers dark mode",
  fact_b: "user prefers light mode",
};

const MIN_RACE_MS = 1100;
const IMPACT_HOLD_MS = 900;

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------------------
// Small presentational pieces (kept local -- not exported, this file's public
// surface is HeadToHead itself).
// ---------------------------------------------------------------------------

function Eyebrow({
  dot,
  label,
  sublabel,
}: {
  dot: "muted" | "live";
  label: string;
  sublabel: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <span
        className={
          dot === "live"
            ? "h-2 w-2 shrink-0 rounded-full bg-accent shadow-[0_0_8px_var(--accent)]"
            : "h-2 w-2 shrink-0 rounded-full bg-foreground-subtle"
        }
        aria-hidden="true"
      />
      <div className="flex flex-col">
        <span className="font-mono text-xs font-medium uppercase tracking-widest text-foreground">
          {label}
        </span>
        <span className="text-[11px] text-foreground-muted">{sublabel}</span>
      </div>
    </div>
  );
}

type AgentState = "pending" | "writing" | "confirmed" | "lost" | "kept";

const AGENT_STATE_CLASSES: Record<AgentState, string> = {
  pending: "border-border bg-background-inset opacity-60",
  writing: "border-border-strong bg-background-inset",
  confirmed: "border-status-ok/40 bg-background-inset",
  lost: "border-status-error/30 bg-background-inset grayscale opacity-40",
  kept: "border-status-conflict/40 bg-background-inset",
};

function AgentCard({
  agentId,
  content,
  state,
  note,
}: {
  agentId: string;
  content: string;
  state: AgentState;
  note?: string;
}) {
  return (
    <div
      className={`min-w-0 rounded-md border px-3 py-2.5 transition-all duration-500 ease-[cubic-bezier(0.2,0,0,1)] ${AGENT_STATE_CLASSES[state]}`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-[10px] uppercase tracking-wider text-foreground-subtle">
          {agentId}
        </span>
        {state === "writing" && (
          <span
            className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-foreground-muted"
            aria-hidden="true"
          />
        )}
        {state === "confirmed" && (
          <span className="text-[10px] font-medium text-status-ok">confirmed</span>
        )}
        {state === "kept" && (
          <span className="text-[10px] font-medium text-status-conflict">kept</span>
        )}
      </div>
      <p
        className={`mt-1 truncate text-sm text-foreground transition-all duration-500 ${
          state === "lost" ? "text-foreground-subtle line-through" : ""
        }`}
        title={content}
      >
        {content}
      </p>
      {note && (
        <p className="mt-1.5 text-[11px] leading-4 text-status-error">{note}</p>
      )}
    </div>
  );
}

function formatTime(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString(undefined, {
      hour12: false,
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

function HistoryRow({ event, index }: { event: MemoryEventRow; index: number }) {
  return (
    <li
      className="flex items-center gap-2 border-t border-border py-1.5 text-[11px] first:border-t-0 opacity-0 animate-[fadeIn_400ms_ease-out_forwards]"
      style={{ animationDelay: `${index * 90}ms` }}
    >
      <span className="w-28 shrink-0 truncate font-mono uppercase tracking-wide text-foreground-subtle">
        {event.op}
      </span>
      <span className="w-6 shrink-0 font-mono text-foreground-muted">
        v{event.new_version}
      </span>
      <span className="w-14 shrink-0 truncate font-mono text-foreground-muted">
        {event.actor_agent}
      </span>
      <span className="min-w-0 flex-1 truncate text-foreground-muted" title={event.payload.content}>
        {event.payload.content}
      </span>
      <span className="shrink-0 font-mono text-foreground-subtle">{formatTime(event.ts)}</span>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function HeadToHead() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [result, setResult] = useState<HeadToHeadResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  const runRace = useCallback(async () => {
    if (phase === "racing" || phase === "impact") return;

    setError(null);
    setResult(null);
    setPhase("racing");

    const startedAt = Date.now();
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const data = await apiFetch<HeadToHeadResult>("/demo/head-to-head", {
        method: "POST",
        body: RACE_PAYLOAD,
        signal: controller.signal,
      });

      const elapsed = Date.now() - startedAt;
      await sleep(Math.max(0, MIN_RACE_MS - elapsed));
      if (!mountedRef.current) return;

      setResult(data);
      setPhase("impact");

      await sleep(IMPACT_HOLD_MS);
      if (!mountedRef.current) return;

      setPhase("resolved");
    } catch (err) {
      if (controller.signal.aborted || !mountedRef.current) return;
      setError(
        err instanceof ApiError
          ? `Backend returned ${err.status}: ${
              typeof err.body === "string" ? err.body : JSON.stringify(err.body)
            }`
          : "Could not reach the Quorum backend. Is it running on :8000?"
      );
      setPhase("idle");
    }
     
  }, [phase]);

  const isRunning = phase === "racing" || phase === "impact";
  const settled = phase === "resolved" && result !== null;

  // Which raced fact the baseline silently dropped, derived from the real
  // response (never assumed) so the UI never claims a specific value was
  // lost unless the backend actually said so.
  const lostIsA = settled && result.baseline.lost_value === result.fact_a;
  const agentAContent = result?.fact_a ?? RACE_PAYLOAD.fact_a;
  const agentBContent = result?.fact_b ?? RACE_PAYLOAD.fact_b;

  const baselineStateA: AgentState =
    phase === "idle"
      ? "pending"
      : isRunning
        ? "writing"
        : lostIsA
          ? "lost"
          : "confirmed";
  const baselineStateB: AgentState =
    phase === "idle"
      ? "pending"
      : isRunning
        ? "writing"
        : lostIsA
          ? "confirmed"
          : "lost";

  const quorumAgentState: AgentState =
    phase === "idle" ? "pending" : isRunning ? "writing" : "kept";

  const finalHistoryVersion =
    result && result.quorum.history.length > 0
      ? result.quorum.history[result.quorum.history.length - 1].new_version
      : null;

  return (
    <section className="w-full" aria-labelledby="head-to-head-title">
      <div className="mb-5 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2
            id="head-to-head-title"
            className="font-mono text-lg font-medium tracking-tight text-foreground"
          >
            Head-to-head: the identical race, two outcomes
          </h2>
          <p className="mt-1 max-w-lg text-sm text-foreground-muted">
            Two agents write conflicting facts to the same key, at the same time.
            Same workload, same cluster -- one side loses data, the other doesn&apos;t.
          </p>
        </div>
        <div className="flex flex-col items-start gap-1.5 sm:items-end">
          <button
            type="button"
            onClick={runRace}
            disabled={isRunning}
            aria-busy={isRunning}
            className="rounded-md bg-accent px-5 py-2.5 font-mono text-sm font-medium text-background-inset transition-all duration-200 ease-[cubic-bezier(0.2,0,0,1)] hover:bg-accent-strong disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-accent"
          >
            {isRunning ? "Racing…" : settled ? "Run it again" : "Run the race"}
          </button>
          <span className="font-mono text-[10px] text-foreground-subtle">
            POST /demo/head-to-head
          </span>
        </div>
      </div>

      <div className="sr-only" role="status" aria-live="polite">
        {phase === "racing" && "Race started. Two agents writing concurrently."}
        {phase === "impact" &&
          "Baseline silently overwrote one fact. Quorum detected the conflict."}
        {phase === "resolved" &&
          "Race resolved. Baseline lost a fact with no trace. Quorum resolved the conflict and kept full history."}
      </div>

      {error && (
        <div className="mb-4 flex items-center justify-between gap-3 rounded-md border border-status-error/40 bg-status-error/10 px-4 py-3 text-sm text-status-error">
          <span className="font-mono">{error}</span>
          <button
            type="button"
            onClick={runRace}
            className="shrink-0 font-mono text-xs underline decoration-status-error/50 underline-offset-2 hover:decoration-status-error"
          >
            retry
          </button>
        </div>
      )}

      <div className="grid grid-cols-1 divide-y divide-border overflow-hidden rounded-lg border border-border bg-background-elevated lg:grid-cols-2 lg:divide-x lg:divide-y-0">
        {/* ------------------------------------------------------------- */}
        {/* LEFT: naive baseline -- last-write-wins, no version guard.    */}
        {/* ------------------------------------------------------------- */}
        <div className="flex flex-col gap-4 p-5">
          <div className="flex items-center justify-between">
            <Eyebrow dot="muted" label="Naive baseline" sublabel="read-modify-write, no version check" />
          </div>

          <p className="font-mono text-[10px] text-foreground-subtle">
            {result ? `${result.scope} / ${result.kind}` : "no scope yet"}
          </p>

          <div className="rounded-md border border-border bg-background-inset px-3 py-2.5">
            <span className="font-mono text-[10px] uppercase tracking-wider text-foreground-subtle">
              current value
            </span>
            <p className="mt-1 truncate font-mono text-sm text-foreground">
              {result && (phase === "impact" || phase === "resolved")
                ? result.baseline.final_value
                : (result?.seed_content ?? RACE_PAYLOAD.seed_content)}
            </p>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <AgentCard
              agentId="agent-a"
              content={agentAContent}
              state={baselineStateA}
              note={
                settled && lostIsA
                  ? "silently overwritten -- no error, no trace"
                  : undefined
              }
            />
            <AgentCard
              agentId="agent-b"
              content={agentBContent}
              state={baselineStateB}
              note={
                settled && !lostIsA
                  ? "silently overwritten -- no error, no trace"
                  : undefined
              }
            />
          </div>

          <div className="rounded-md border border-dashed border-border px-3 py-4 text-center">
            <p className="text-[11px] leading-5 text-foreground-subtle">
              {settled
                ? "No history. No conflict record. The overwritten fact leaves nothing behind to inspect."
                : "No history panel -- this store keeps none."}
            </p>
          </div>
        </div>

        {/* ------------------------------------------------------------- */}
        {/* RIGHT: Quorum -- SERIALIZABLE + optimistic CAS.               */}
        {/* ------------------------------------------------------------- */}
        <div className="relative flex flex-col gap-4 p-5">
          <div className="flex items-center justify-between">
            <Eyebrow dot="live" label="Quorum" sublabel="CockroachDB SERIALIZABLE + optimistic CAS" />
          </div>

          <p className="font-mono text-[10px] text-foreground-subtle">
            {result ? `${result.scope} / ${result.kind}` : "no scope yet"}
          </p>

          <div className="relative rounded-md border border-border bg-background-inset px-3 py-2.5">
            <div className="flex items-center justify-between">
              <span className="font-mono text-[10px] uppercase tracking-wider text-foreground-subtle">
                current value
              </span>
              {settled && (
                <span className="font-mono text-[10px] text-status-conflict">
                  resolved{finalHistoryVersion !== null ? ` · v${finalHistoryVersion}` : ""}
                </span>
              )}
            </div>
            <p className="mt-1 break-words font-mono text-sm text-foreground">
              {result && (phase === "impact" || phase === "resolved")
                ? result.quorum.final_value
                : (result?.seed_content ?? RACE_PAYLOAD.seed_content)}
            </p>

            {phase === "impact" && (
              <div className="absolute inset-x-0 -top-3 flex justify-center">
                <span className="animate-[popIn_250ms_cubic-bezier(0.2,0,0,1)_forwards] rounded-full border border-status-conflict bg-background px-3 py-1 font-mono text-[11px] font-semibold uppercase tracking-wider text-status-conflict shadow-[0_0_12px_rgba(240,177,85,0.35)]">
                  Conflict detected
                </span>
              </div>
            )}
          </div>

          <div className="grid grid-cols-2 gap-2">
            <AgentCard agentId="agent-a" content={agentAContent} state={quorumAgentState} />
            <AgentCard agentId="agent-b" content={agentBContent} state={quorumAgentState} />
          </div>

          <div className="rounded-md border border-border bg-background-inset px-3 py-2.5">
            <span className="font-mono text-[10px] uppercase tracking-wider text-foreground-subtle">
              history
            </span>
            {settled && result.quorum.history.length > 0 ? (
              <ul className="mt-1.5">
                {result.quorum.history.map((event, i) => (
                  <HistoryRow key={event.event_id} event={event} index={i} />
                ))}
              </ul>
            ) : (
              <p className="mt-1.5 text-[11px] text-foreground-subtle">
                {settled ? "no events recorded" : "populates once the race resolves"}
              </p>
            )}
          </div>
        </div>
      </div>

      <style>{`
        @keyframes fadeIn {
          from { opacity: 0; transform: translateY(2px); }
          to { opacity: 1; transform: translateY(0); }
        }
        @keyframes popIn {
          from { opacity: 0; transform: scale(0.92); }
          to { opacity: 1; transform: scale(1); }
        }
      `}</style>
    </section>
  );
}
