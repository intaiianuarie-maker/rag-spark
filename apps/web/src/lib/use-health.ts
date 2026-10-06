"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { parseHealthApiResponse, type HealthStatus } from "@/lib/health";

/**
 * Client state machine for the status page:
 * - loading: no data yet (first fetch in flight)
 * - ready:   backend answered (health may still be "empty" — rendered as an
 *            empty state by the panel)
 * - error:   backend unreachable / unhealthy proxy response
 *
 * Deliberately hand-rolled instead of adding a data-fetching library (SWR /
 * TanStack Query): M0 has exactly one endpoint and the coding standards call
 * for the least custom code that satisfies the requirement. Revisit if more
 * data-driven views arrive in M1+.
 */

export type HealthUiState =
  | { phase: "loading" }
  | { phase: "ready"; health: HealthStatus; fetchedAt: Date }
  | { phase: "error"; message: string; status?: number };

const POLL_INTERVAL_MS = 15_000;

export function useHealthStatus(pollIntervalMs: number = POLL_INTERVAL_MS): {
  state: HealthUiState;
  refresh: () => void;
  isRefreshing: boolean;
} {
  const [state, setState] = useState<HealthUiState>({ phase: "loading" });
  const [isRefreshing, setIsRefreshing] = useState(false);
  const inFlight = useRef(false);

  const load = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setIsRefreshing(true);
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      const parsed = parseHealthApiResponse(await response.json().catch(() => null));
      if (parsed === null) {
        setState({
          phase: "error",
          message: `Received an unexpected response from /api/health (HTTP ${response.status}).`,
          status: response.status,
        });
      } else if (parsed.ok) {
        setState({
          phase: "ready",
          health: parsed.health,
          fetchedAt: new Date(parsed.fetchedAt),
        });
      } else if (parsed.health && !parsed.health.isEmpty) {
        // Backend answered with a health payload but a non-2xx status
        // (e.g. HTTP 503 during startup): show data with a "down" treatment.
        setState({ phase: "ready", health: parsed.health, fetchedAt: new Date() });
      } else {
        setState({ phase: "error", message: parsed.message, status: parsed.status });
      }
    } catch {
      setState({
        phase: "error",
        message: "Could not load /api/health (the web app route handler failed).",
      });
    } finally {
      inFlight.current = false;
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    // Initial fetch and polling both run in scheduler callbacks (not in the
    // effect body), keeping state updates out of synchronous render per the
    // react-hooks/set-state-in-effect rule.
    const tick = () => {
      if (typeof document !== "undefined" && document.hidden) return;
      void load();
    };
    const initialTimer = setTimeout(tick, 0);
    const interval = setInterval(tick, pollIntervalMs);
    const onVisible = () => {
      if (!document.hidden) void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearTimeout(initialTimer);
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load, pollIntervalMs]);

  return { state, refresh: () => void load(), isRefreshing };
}
