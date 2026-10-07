"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  listMyWorkspaces,
  type WorkspacePublic,
} from "./contracts";

/**
 * Client workspace context (TASK-M1-002b).
 *
 * Loads the caller's workspaces on mount and after any mutation; tracks the
 * active workspace (persisted as a UI preference — never a credential).
 * States:
 *  - loading: initial fetch in flight
 *  - anonymous: 401/login required (public pages must NOT redirect) → the
 *    header switcher renders nothing
 *  - error: unexpected failure (retry affordance in the switcher)
 *  - ready: list available (possibly empty)
 */

type ProviderState = {
  status: "loading" | "anonymous" | "error" | "ready";
  workspaces: WorkspacePublic[];
  activeId: string | null;
  active: WorkspacePublic | null;
  setActive: (workspaceId: string) => void;
  reload: () => Promise<void>;
  myRole: (workspaceId?: string | null) => WorkspacePublic["role"] | null;
};

const WorkspaceContext = createContext<ProviderState | null>(null);

const ACTIVE_WS_STORAGE_KEY = "kios_active_ws";

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<ProviderState["status"]>("loading");
  const [workspaces, setWorkspaces] = useState<WorkspacePublic[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const inFlight = useRef(false);

  const reload = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const list = await listMyWorkspaces();
      setWorkspaces(list);
      setStatus("ready");
      setActiveId((current) => {
        if (current && list.some((ws) => ws.id === current)) return current;
        const stored = readStoredActive(list);
        return stored ?? list[0]?.id ?? null;
      });
    } catch (error: unknown) {
      if (error instanceof Error && error.name === "AuthLoginRequiredError") {
        setWorkspaces([]);
        setActiveId(null);
        setStatus("anonymous");
      } else {
        setStatus("error");
      }
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    // Initial load in a scheduler callback (not in the effect body), keeping
    // state updates out of synchronous render (react-hooks/set-state-in-effect).
    const initialTimer = setTimeout(() => void reload(), 0);
    return () => clearTimeout(initialTimer);
  }, [reload]);

  const setActive = useCallback((workspaceId: string) => {
    setActiveId(workspaceId);
    try {
      window.localStorage.setItem(ACTIVE_WS_STORAGE_KEY, workspaceId);
    } catch {
      // Private-mode storage quota: selection just won't persist.
    }
  }, []);

  const myRole = useCallback(
    (workspaceId?: string | null) => {
      const id = workspaceId ?? activeId;
      if (!id) return null;
      return workspaces.find((ws) => ws.id === id)?.role ?? null;
    },
    [workspaces, activeId],
  );

  const active = activeId
    ? (workspaces.find((ws) => ws.id === activeId) ?? null)
    : null;

  return (
    <WorkspaceContext.Provider
      value={{
        status,
        workspaces,
        activeId,
        active,
        setActive,
        reload,
        myRole,
      }}
    >
      {children}
    </WorkspaceContext.Provider>
  );
}

function readStoredActive(list: WorkspacePublic[]): string | null {
  try {
    const stored = window.localStorage.getItem(ACTIVE_WS_STORAGE_KEY);
    if (stored && list.some((ws) => ws.id === stored)) return stored;
  } catch {
    return null;
  }
  return null;
}

export function useWorkspaces(): ProviderState {
  const context = useContext(WorkspaceContext);
  if (!context) {
    throw new Error("useWorkspaces must be used inside <WorkspaceProvider>");
  }
  return context;
}
