"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useWorkspaces } from "@/components/workspace/workspace-provider";
import {
  listSources,
  createSource,
  updateSource,
  deleteSource,
  triggerSourceSync,
  getSourceStatus,
  type Source,
  type SourceKind,
  type SourceStatus,
} from "./contracts";

const KINDS: SourceKind[] = ["local_upload", "web", "s3", "git"];

type FormState = {
  connector_id: string;
  kind: SourceKind;
  settings: Record<string, unknown>;
};

const emptyForm = (): FormState => ({
  connector_id: "",
  kind: "local_upload",
  settings: {},
});

export default function SourcesPage() {
  const { activeId } = useWorkspaces();
  return <ScopedSourcesPage key={activeId ?? "no-workspace"} activeId={activeId} />;
}

function ScopedSourcesPage({ activeId }: { activeId: string | null }) {
  const activeIdRef = useRef(activeId);
  const aliveRef = useRef(true);
  const requestEpochRef = useRef(0);
  const listRequestRef = useRef(0);
  const pollTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const pollGenerationRef = useRef<Record<string, number>>({});

  useEffect(() => { activeIdRef.current = activeId; }, [activeId]);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  const [status, setStatus] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(emptyForm());
  const [editing, setEditing] = useState<string | null>(null);
  const [scope, setScope] = useState<{ ws: string | null; sources: Source[] }>({ ws: null, sources: [] });
  const [polling, setPolling] = useState<Record<string, SourceStatus>>({});

  const dataWs = scope.ws === activeId ? scope : null;
  const sources = dataWs ? dataWs.sources : [];

  const clearPoll = useCallback((id: string) => {
    const t = pollTimersRef.current[id];
    if (t) {
      clearTimeout(t);
      delete pollTimersRef.current[id];
    }
  }, []);

  const clearAllPolls = useCallback(() => {
    Object.keys(pollTimersRef.current).forEach((id) => clearPoll(id));
  }, [clearPoll]);

  useEffect(() => {
    return () => {
      aliveRef.current = false;
      clearAllPolls();
    };
  }, [clearAllPolls]);

  const refresh = useCallback(async (epoch: number) => {
    const ws = activeIdRef.current;
    if (!ws || !aliveRef.current) return;
    const request = ++listRequestRef.current;
    try {
      const ss = await listSources(ws);
      if (!aliveRef.current) return;
      if (request !== listRequestRef.current) return;
      if (epoch !== requestEpochRef.current) return;
      if (activeIdRef.current !== ws) return;
      setScope({ ws, sources: ss });
    } catch (e) {
      if (!aliveRef.current) return;
      if (request !== listRequestRef.current) return;
      if (epoch !== requestEpochRef.current) return;
      if (activeIdRef.current !== ws) return;
      setStatus(e instanceof Error ? e.message : "load failed");
    }
  }, []);

  useEffect(() => {
    const epoch = ++requestEpochRef.current;
    clearAllPolls();
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setScope({ ws: activeId, sources: [] });
    setPolling({});
    setForm(emptyForm());
    setEditing(null);
    setStatus(null);
    void refresh(epoch);
    return () => {
      aliveRef.current = false;
    };
  }, [activeId, refresh, clearAllPolls]);


  const startPolling = useCallback((ws: string, id: string, gen: number, previousAttempt: string | null) => {
    if (!aliveRef.current) return;
    clearPoll(id);
    pollGenerationRef.current[id] = gen;

    const tick = async () => {
      if (!aliveRef.current) { clearPoll(id); return; }
      if (activeIdRef.current !== ws) { clearPoll(id); return; }
      if (pollGenerationRef.current[id] !== gen) return;
      try {
        const st = await getSourceStatus(ws, id);
        if (!aliveRef.current) return;
        if (activeIdRef.current !== ws) return;
        if (pollGenerationRef.current[id] !== gen) return;
        setPolling((prev) => ({ ...prev, [id]: st }));
        // Terminal only when the server reports a NEW attempt (last_attempt_at
        // differs from the pre-trigger value) and a terminal outcome.
        const newAttempt = !!st.last_attempt_at && st.last_attempt_at !== previousAttempt;
        const succeeded = !!st.last_success_at && !!st.last_attempt_at &&
          Date.parse(st.last_success_at) >= Date.parse(st.last_attempt_at);
        const terminal = newAttempt && (!!st.last_error || succeeded);
        if (terminal) {
          clearPoll(id);
          const epoch = requestEpochRef.current;
          void refresh(epoch);
          return;
        }
        // Chain requests after completion so a slow poll cannot overlap a
        // newer response and overwrite a terminal state.
        pollTimersRef.current[id] = setTimeout(() => { void tick(); }, 2000);
      } catch (e) {
        if (!aliveRef.current) return;
        if (activeIdRef.current !== ws) return;
        if (pollGenerationRef.current[id] !== gen) return;
        setStatus(e instanceof Error ? e.message : "status failed");
        clearPoll(id);
      }
    };
    void tick();
  }, [clearPoll, refresh]);

  const handleCreate = useCallback(async () => {
    const ws = activeIdRef.current;
    if (!ws || !form.connector_id.trim()) {
      setStatus("workspace and id required");
      return;
    }
    const epoch = requestEpochRef.current;
    try {
      await createSource(ws, {
        connector_id: form.connector_id.trim(),
        kind: form.kind,
        settings: form.settings,
      });
      if (!aliveRef.current || epoch !== requestEpochRef.current || activeIdRef.current !== ws) return;
      setForm(emptyForm());
      setStatus(null);
      await refresh(epoch);
    } catch (e) {
      if (!aliveRef.current || activeIdRef.current !== ws) return;
      setStatus(e instanceof Error ? e.message : "create failed");
    }
  }, [form, refresh]);

  const handleUpdate = useCallback(async () => {
    const ws = activeIdRef.current;
    if (!ws || !editing) return;
    const epoch = requestEpochRef.current;
    try {
      await updateSource(ws, editing, { settings: form.settings });
      if (!aliveRef.current || epoch !== requestEpochRef.current || activeIdRef.current !== ws) return;
      setEditing(null);
      setForm(emptyForm());
      setStatus(null);
      await refresh(epoch);
    } catch (e) {
      if (!aliveRef.current || activeIdRef.current !== ws) return;
      setStatus(e instanceof Error ? e.message : "update failed");
    }
  }, [editing, form, refresh]);

  const handleDelete = useCallback(async (id: string) => {
    const ws = activeIdRef.current;
    if (!ws) return;
    const epoch = requestEpochRef.current;
    const gen = (pollGenerationRef.current[id] ?? 0) + 1;
    pollGenerationRef.current[id] = gen;
    clearPoll(id);
    setPolling((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    try {
      await deleteSource(ws, id);
      if (!aliveRef.current || epoch !== requestEpochRef.current || activeIdRef.current !== ws) return;
      await refresh(epoch);
    } catch (e) {
      if (!aliveRef.current || activeIdRef.current !== ws) return;
      setStatus(e instanceof Error ? e.message : "delete failed");
    }
  }, [refresh, clearPoll]);

  const handleSync = useCallback(async (id: string) => {
    const ws = activeIdRef.current;
    if (!ws) return;
    const epoch = requestEpochRef.current;
    const gen = (pollGenerationRef.current[id] ?? 0) + 1;
    pollGenerationRef.current[id] = gen;
    clearPoll(id);
    try {
      const baseline = await getSourceStatus(ws, id);
      if (!aliveRef.current || epoch !== requestEpochRef.current || activeIdRef.current !== ws) return;
      if (pollGenerationRef.current[id] !== gen) return;
      await triggerSourceSync(ws, id);
      if (!aliveRef.current || epoch !== requestEpochRef.current || activeIdRef.current !== ws) return;
      if (pollGenerationRef.current[id] !== gen) return;
      startPolling(ws, id, gen, baseline.last_attempt_at);
    } catch (e) {
      if (!aliveRef.current || activeIdRef.current !== ws) return;
      setStatus(e instanceof Error ? e.message : "sync failed");
    }
  }, [startPolling, clearPoll]);

  const handleEdit = useCallback((s: Source) => {
    setEditing(s.connector_id);
    setForm({ connector_id: s.connector_id, kind: s.kind, settings: s.settings || {} });
  }, []);

  const handleCancelEdit = useCallback(() => {
    setEditing(null);
    setForm(emptyForm());
  }, []);

  const updateSettingsField = useCallback((key: string, value: string) => {
    setForm((prev) => {
      let parsed: unknown = value;
      if (key === "politeness_delay_ms" || key === "politeness_delay" || key === "max_redirects") {
        if (!value.trim()) {
          const settings = { ...prev.settings };
          delete settings[key];
          return { ...prev, settings };
        }
        const n = Number(value);
        if (value.trim() !== "" && Number.isFinite(n)) parsed = n;
      } else if (key === "urls" || key === "allowed_origins") {
        try { parsed = JSON.parse(value); } catch { parsed = value; }
        if (!Array.isArray(parsed)) parsed = value;
      } else {
        parsed = value;
      }
      return { ...prev, settings: { ...prev.settings, [key]: parsed } };
    });
  }, []);

  if (!activeId) {
    return <div className="p-6 text-gray-500" role="status">Select a workspace to manage sources.</div>;
  }

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-2xl font-bold">Sources</h1>
      {status && <div role="alert" className="text-red-600 text-sm">{status}</div>}

      <section className="space-y-2 border rounded p-4" aria-label="add-connector">
        <h2 className="font-semibold">{editing ? "Edit connector" : "Add connector"}</h2>
        <div className="flex gap-2">
          <label className="sr-only" htmlFor="connector-id">Connector id</label>
          <input
            id="connector-id"
            className="border rounded px-2 py-1 flex-1"
            placeholder="connector id"
            value={form.connector_id}
            disabled={!!editing}
            onChange={(e) => setForm((f) => ({ ...f, connector_id: e.target.value }))}
          />
          <label className="sr-only" htmlFor="connector-kind">Kind</label>
          <select
            id="connector-kind"
            className="border rounded px-2 py-1"
            value={form.kind}
            disabled={!!editing}
            onChange={(e) => setForm((f) => ({ ...f, kind: e.target.value as SourceKind, settings: {} }))}
          >
            {KINDS.map((k) => (
              <option key={k} value={k}>{k}</option>
            ))}
          </select>
        </div>
        <div className="grid grid-cols-2 gap-2 text-sm">
          {form.kind === "local_upload" && (
            <input aria-label="local path" className="border rounded px-2 py-1" placeholder="path" value={String(form.settings.path ?? "")}
              onChange={(e) => updateSettingsField("path", e.target.value)} />
          )}
          {form.kind === "web" && (
            <>
              <input aria-label="web urls" className="border rounded px-2 py-1 col-span-2" placeholder="urls (comma separated)" value={Array.isArray(form.settings.urls) ? (form.settings.urls as string[]).join(",") : ""}
                onChange={(e) => updateSettingsField("urls", JSON.stringify(e.target.value.split(",").map((x) => x.trim()).filter(Boolean)))} />
              <input aria-label="web allowed origins" className="border rounded px-2 py-1" placeholder="allowed_origins (comma)" value={Array.isArray(form.settings.allowed_origins) ? (form.settings.allowed_origins as string[]).join(",") : ""}
                onChange={(e) => updateSettingsField("allowed_origins", JSON.stringify(e.target.value.split(",").map((x) => x.trim()).filter(Boolean)))} />
              <input aria-label="web politeness delay ms" className="border rounded px-2 py-1" placeholder="politeness_delay_ms" value={form.settings.politeness_delay_ms != null ? String(form.settings.politeness_delay_ms) : ""}
                onChange={(e) => updateSettingsField("politeness_delay_ms", e.target.value)} />
            </>
          )}
          {form.kind === "s3" && (
            <>
              <input aria-label="s3 bucket" className="border rounded px-2 py-1" placeholder="bucket" value={String(form.settings.bucket ?? "")}
                onChange={(e) => updateSettingsField("bucket", e.target.value)} />
              <input aria-label="s3 prefix" className="border rounded px-2 py-1" placeholder="prefix" value={String(form.settings.prefix ?? "")}
                onChange={(e) => updateSettingsField("prefix", e.target.value)} />
            </>
          )}
          {form.kind === "git" && (
            <>
              <input aria-label="git repo" className="border rounded px-2 py-1" placeholder="repo" value={String(form.settings.repo ?? "")}
                onChange={(e) => updateSettingsField("repo", e.target.value)} />
              <input aria-label="git ref" className="border rounded px-2 py-1" placeholder="ref" value={String(form.settings.ref ?? "HEAD")}
                onChange={(e) => updateSettingsField("ref", e.target.value)} />
            </>
          )}
        </div>
        <div className="flex gap-2">
          {editing ? (
            <>
              <button className="bg-blue-600 text-white px-3 py-1 rounded" onClick={handleUpdate} aria-label="save connector">Save</button>
              <button className="px-3 py-1 rounded border" onClick={handleCancelEdit} aria-label="cancel edit">Cancel</button>
            </>
          ) : (
            <button className="bg-blue-600 text-white px-3 py-1 rounded" onClick={handleCreate} aria-label="add connector">Add</button>
          )}
        </div>
      </section>

      <section className="space-y-2">
        <h2 className="font-semibold">Connectors</h2>
        {sources.length === 0 && <div className="text-gray-500 text-sm">No connectors.</div>}
        {sources.map((s) => {
          const st = polling[s.connector_id];
          return (
            <div key={s.connector_id} className="border rounded p-4 space-y-2" data-testid={`source-${s.connector_id}`}>
              <div className="flex items-center justify-between">
                <div>
                  <span className="font-mono font-semibold">{s.connector_id}</span>
                  <span className="ml-2 text-xs text-gray-500">{s.kind}</span>
                </div>
                <div className="flex gap-2">
                  <button aria-label={`edit ${s.connector_id}`} className="text-sm px-2 py-1 border rounded" onClick={() => handleEdit(s)}>
                    Edit
                  </button>
                  <button aria-label={`sync ${s.connector_id}`} className="text-sm px-2 py-1 border rounded" onClick={() => handleSync(s.connector_id)}>
                    Sync
                  </button>
                  <button aria-label={`delete ${s.connector_id}`} className="text-sm px-2 py-1 border rounded text-red-600" onClick={() => handleDelete(s.connector_id)}>
                    Delete
                  </button>
                </div>
              </div>
              {s.last_error && (
                <div role="alert" className="text-red-600 text-sm">
                  Error: {s.last_error}
                  <button className="ml-2 underline" onClick={() => handleSync(s.connector_id)} aria-label={`retry ${s.connector_id}`}>Retry</button>
                </div>
              )}
              {st && (
                <div className="text-xs text-gray-600" data-testid={`status-${s.connector_id}`}>
                  seen {st.items_seen} created {st.items_created} updated {st.items_updated} unchanged {st.items_unchanged} failed {st.items_failed} deleted {st.items_deleted}
                  {st.last_success_at && <span className="ml-2">last success {st.last_success_at}</span>}
                  {st.last_error && <span className="ml-2 text-red-600">error {st.last_error}</span>}
                </div>
              )}
              {!st && s.last_success_at && (
                <div className="text-xs text-gray-600">last success {s.last_success_at}</div>
              )}
              <pre className="text-xs bg-gray-100 p-2 rounded overflow-auto">{JSON.stringify(s.settings)}</pre>
            </div>
          );
        })}
      </section>
    </div>
  );
}
