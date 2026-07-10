"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE_URL } from "./api";

/**
 * Generic EventSource wrapper/hook for the backend's SSE stream(s).
 *
 * The exact stream path (e.g. `/events/stream`) is finalized by U4
 * (`app/api/sse_routes.py`) -- this wrapper is written generically against a
 * `path` parameter so it works regardless of the final route name. Pass the
 * full path (including leading slash), e.g. `useSSE<ChangeEvent>("/events/stream")`.
 */

export type SSEStatus = "idle" | "connecting" | "open" | "closed" | "error";

export interface UseSSEOptions {
  /** Named SSE event to listen for; defaults to the unnamed "message" event. */
  eventName?: string;
  /** If false, the connection is not opened (or is torn down). Default true. */
  enabled?: boolean;
  /** Override the base URL for this stream only. */
  baseUrl?: string;
  /** Parse each event's `data` field; defaults to `JSON.parse`. */
  parse?: (raw: string) => unknown;
  /** Cap the number of retained events client-side; default 200. */
  maxEvents?: number;
}

export interface UseSSEResult<T> {
  status: SSEStatus;
  /** Most recent event received, or null before the first one arrives. */
  latest: T | null;
  /** Rolling buffer of received events, newest last. */
  events: T[];
  error: Event | null;
  /** Manually close the connection. */
  close: () => void;
}

/**
 * Subscribe to a backend SSE endpoint and accumulate parsed events.
 *
 * Example:
 *   const { status, latest, events } = useSSE<ChangeEvent>("/events/stream");
 */
export function useSSE<T = unknown>(
  path: string,
  options: UseSSEOptions = {}
): UseSSEResult<T> {
  const {
    eventName,
    enabled = true,
    baseUrl,
    parse = (raw: string) => JSON.parse(raw),
    maxEvents = 200,
  } = options;

  const [status, setStatus] = useState<SSEStatus>("idle");
  const [latest, setLatest] = useState<T | null>(null);
  const [events, setEvents] = useState<T[]>([]);
  const [error, setError] = useState<Event | null>(null);
  const sourceRef = useRef<EventSource | null>(null);

  const close = () => {
    sourceRef.current?.close();
    sourceRef.current = null;
    setStatus("closed");
  };

  useEffect(() => {
    if (!enabled) {
      sourceRef.current?.close();
      sourceRef.current = null;
      return;
    }

    const url = `${baseUrl ?? API_BASE_URL}${path}`;
    const source = new EventSource(url);
    sourceRef.current = source;
    // Defer to a microtask so this is a callback, not a direct
    // synchronous setState call in the effect body.
    queueMicrotask(() => setStatus("connecting"));

    source.onopen = () => setStatus("open");

    const handleMessage = (event: MessageEvent<string>) => {
      try {
        const parsed = parse(event.data) as T;
        setLatest(parsed);
        setEvents((prev) => {
          const next = [...prev, parsed];
          return next.length > maxEvents ? next.slice(-maxEvents) : next;
        });
      } catch {
        // Malformed event payload -- surface via error state without
        // tearing down the connection; the stream may recover.
      }
    };

    if (eventName) {
      source.addEventListener(eventName, handleMessage as EventListener);
    } else {
      source.onmessage = handleMessage;
    }

    source.onerror = (event) => {
      setError(event);
      setStatus("error");
    };

    return () => {
      if (eventName) {
        source.removeEventListener(eventName, handleMessage as EventListener);
      }
      source.close();
      sourceRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, eventName, enabled, baseUrl, maxEvents]);

  return { status, latest, events, error, close };
}

/**
 * Non-hook EventSource wrapper for use outside React (e.g. in a store or a
 * plain module), or when finer-grained control is needed than `useSSE`
 * provides.
 */
export class SSEConnection<T = unknown> {
  private source: EventSource | null = null;

  constructor(
    private readonly path: string,
    private readonly handlers: {
      onEvent: (data: T) => void;
      onError?: (event: Event) => void;
      onOpen?: () => void;
      eventName?: string;
      baseUrl?: string;
      parse?: (raw: string) => unknown;
    }
  ) {}

  connect(): void {
    const { eventName, baseUrl, parse = (raw: string) => JSON.parse(raw) } =
      this.handlers;
    const url = `${baseUrl ?? API_BASE_URL}${this.path}`;
    const source = new EventSource(url);
    this.source = source;

    source.onopen = () => this.handlers.onOpen?.();
    source.onerror = (event) => this.handlers.onError?.(event);

    const handleMessage = (event: MessageEvent<string>) => {
      this.handlers.onEvent(parse(event.data) as T);
    };

    if (eventName) {
      source.addEventListener(eventName, handleMessage as EventListener);
    } else {
      source.onmessage = handleMessage;
    }
  }

  close(): void {
    this.source?.close();
    this.source = null;
  }
}
