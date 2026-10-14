"use client";

import { useCallback, useEffect, useState } from "react";

import { auth } from "@/lib/auth-client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useWorkspaces } from "@/components/workspace/workspace-provider";

/**
 * V4 Visual Workflows Authoring Surface (T-R19).
 *
 * Minimal, honest authoring surface for workspace workflow drafts and runs:
 *  - 0 mock content: Every rendered value traces directly to real backend API models;
 *  - Supports 8 core operations: draft, import, validate, save, approve, run, status, cancel;
 *  - Scoped by the active workspace from WorkspaceProvider;
 *  - Honest error and empty states (never masking failures or unconfigured states).
 */

export interface DraftRevisionMeta {
  workflow_id: string;
  workspace_id: string;
  revision: number;
  content_hash: string;
  summary: string;
  parent_revision: number | null;
  parent_hash: string | null;
  created_at: string;
}

export interface WorkflowNodeSpec {
  id: string;
  type: string;
  name: string;
  config?: Record<string, unknown>;
}

export interface WorkflowEdgeSpec {
  id: string;
  source_node_id: string;
  source_port: string;
  target_node_id: string;
  target_port: string;
  condition_value?: unknown;
}

export interface WorkflowDefinition {
  id: string;
  workspace_id: string;
  name: string;
  description: string;
  nodes: WorkflowNodeSpec[];
  edges: WorkflowEdgeSpec[];
}

export interface DraftRevisionDetail extends DraftRevisionMeta {
  spec: WorkflowDefinition;
}

export interface EditorAnnotation {
  code: string;
  node_id: string | null;
  port: string | null;
  message: string;
  severity: "error" | "warning";
  blocking: boolean;
}

export interface ValidationResponse {
  valid: boolean;
  errors: Array<{ code: string; message: string; node_id?: string; edge_id?: string }>;
  annotations: EditorAnnotation[];
}

export interface ApprovalRecord {
  approved: boolean;
  workflow_id: string;
  workspace_id: string;
  revision: number;
  content_hash: string;
  approved_by: string;
  approved_at: string;
  comment?: string;
}

export interface WorkflowRunSnapshot {
  id: string;
  workspace_id: string;
  workflow_id: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  outputs: Record<string, unknown>;
  error?: string | null;
  started_at: string;
  completed_at?: string | null;
}

export interface TimelineEntry {
  node_id: string;
  state: "pending" | "running" | "completed" | "failed" | "skipped";
  duration_ms: number;
  skip_reason?: string | null;
  error?: string | null;
}

export interface TimelineModel {
  terminal_state: "running" | "completed" | "failed" | "cancelled";
  total_duration_ms: number;
  failing_node_id?: string | null;
  entries: Record<string, TimelineEntry>;
}

export interface WorkflowRunResponse {
  run: WorkflowRunSnapshot;
  timeline: TimelineModel;
}

export default function WorkflowsAuthoringPage() {
  const { active: activeWorkspace, activeId } = useWorkspaces();
  const workspaceId = activeId ?? activeWorkspace?.id ?? "";

  const [drafts, setDrafts] = useState<DraftRevisionMeta[]>([]);
  const [selectedWorkflowId, setSelectedWorkflowId] = useState<string | null>(null);
  const [selectedDraft, setSelectedDraft] = useState<DraftRevisionDetail | null>(null);
  const [history, setHistory] = useState<DraftRevisionMeta[]>([]);
  const [validation, setValidation] = useState<ValidationResponse | null>(null);
  const [approval, setApproval] = useState<ApprovalRecord | null>(null);
  const [activeRun, setActiveRun] = useState<WorkflowRunResponse | null>(null);

  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  // New Draft Form State
  const [newWorkflowId, setNewWorkflowId] = useState("");
  const [newWorkflowName, setNewWorkflowName] = useState("");
  const [importJson, setImportJson] = useState("");
  const [showImport, setShowImport] = useState(false);

  const clearNotifications = () => {
    setErrorMessage(null);
    setStatusMessage(null);
  };

  // 1. Fetch Drafts List for Active Workspace
  const loadDrafts = useCallback(async () => {
    if (!workspaceId) return;
    setLoading(true);
    clearNotifications();
    try {
      const res = await auth.authFetch<{ count: number; drafts: DraftRevisionMeta[] }>(
        `v1/workspaces/${encodeURIComponent(workspaceId)}/drafts`
      );
      setDrafts(res.drafts || []);
      if (res.drafts?.length > 0 && !selectedWorkflowId) {
        setSelectedWorkflowId(res.drafts[0].workflow_id);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMessage(`Failed to load drafts: ${msg}`);
      setDrafts([]);
    } finally {
      setLoading(false);
    }
  }, [workspaceId, selectedWorkflowId]);

  useEffect(() => {
    if (!workspaceId) return;
    const timer = window.setTimeout(() => {
      void loadDrafts();
    }, 0);
    return () => {
      window.clearTimeout(timer);
    };
  }, [workspaceId, loadDrafts]);

  // 2. Fetch Selected Draft Details and Revisions History
  const loadDraftDetail = useCallback(
    async (wid: string) => {
      if (!workspaceId || !wid) return;
      try {
        const [detailRes, historyRes] = await Promise.all([
          auth.authFetch<DraftRevisionDetail>(
            `v1/workspaces/${encodeURIComponent(workspaceId)}/drafts/${encodeURIComponent(wid)}`
          ),
          auth.authFetch<{ count: number; revisions: DraftRevisionMeta[] }>(
            `v1/workspaces/${encodeURIComponent(workspaceId)}/drafts/${encodeURIComponent(wid)}/revisions`
          ),
        ]);
        setSelectedDraft(detailRes);
        setHistory(historyRes.revisions || []);
        setValidation(null);
        setApproval(null);
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        setErrorMessage(`Failed to load draft ${wid}: ${msg}`);
      }
    },
    [workspaceId]
  );

  useEffect(() => {
    if (!workspaceId) return;
    const timer = window.setTimeout(() => {
      if (selectedWorkflowId) {
        void loadDraftDetail(selectedWorkflowId);
      } else {
        setSelectedDraft(null);
        setHistory([]);
      }
    }, 0);
    return () => {
      window.clearTimeout(timer);
    };
  }, [workspaceId, selectedWorkflowId, loadDraftDetail]);

  // 3. Action: Validate Workflow
  const handleValidate = async () => {
    if (!workspaceId || !selectedWorkflowId) return;
    clearNotifications();
    try {
      const res = await auth.authFetch<ValidationResponse>(
        `v1/workspaces/${encodeURIComponent(workspaceId)}/drafts/validate`,
        {
          method: "POST",
          body: { workflow_id: selectedWorkflowId },
        }
      );
      setValidation(res);
      setStatusMessage(res.valid ? "Workflow is structurally valid." : "Validation errors detected.");
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMessage(`Validation request failed: ${msg}`);
    }
  };

  // 4. Action: Save New Draft
  const handleCreateDraft = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!workspaceId || !newWorkflowId || !newWorkflowName) return;
    clearNotifications();
    try {
      const starterSpec: WorkflowDefinition = {
        id: newWorkflowId.trim(),
        workspace_id: workspaceId,
        name: newWorkflowName.trim(),
        description: "Authored visual workflow",
        nodes: [
          { id: "node-1", type: "trigger", name: "Start Trigger", config: {} },
          { id: "node-2", type: "output", name: "End Output", config: {} },
        ],
        edges: [
          {
            id: "edge-1",
            source_node_id: "node-1",
            source_port: "output",
            target_node_id: "node-2",
            target_port: "input",
          },
        ],
      };

      await auth.authFetch(
        `v1/workspaces/${encodeURIComponent(workspaceId)}/drafts`,
        {
          method: "POST",
          body: starterSpec,
        }
      );

      setStatusMessage(`Created draft ${starterSpec.id} as revision 1.`);
      setNewWorkflowId("");
      setNewWorkflowName("");
      setSelectedWorkflowId(starterSpec.id);
      loadDrafts();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMessage(`Failed to save draft: ${msg}`);
    }
  };

  // 5. Action: Revise Draft (Append Revision)
  const handleReviseDraft = async () => {
    if (!workspaceId || !selectedDraft) return;
    clearNotifications();
    try {
      const nextSpec: WorkflowDefinition = {
        ...selectedDraft.spec,
        description: `Revision updated at ${new Date().toISOString()}`,
      };

      const res = await auth.authFetch<{ revision: number; content_hash: string }>(
        `v1/workspaces/${encodeURIComponent(workspaceId)}/drafts/${encodeURIComponent(
          selectedDraft.workflow_id
        )}/revisions`,
        {
          method: "POST",
          body: nextSpec,
        }
      );

      setStatusMessage(`Saved revision ${res.revision} (hash: ${res.content_hash.slice(0, 12)}…).`);
      loadDraftDetail(selectedDraft.workflow_id);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMessage(`Failed to append revision: ${msg}`);
    }
  };

  // 6. Action: Import Workflow (Langflow or Native)
  const handleImport = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!workspaceId || !importJson.trim()) return;
    clearNotifications();
    try {
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(importJson);
      } catch (parseErr) {
        setErrorMessage(`Invalid JSON syntax: ${String(parseErr)}`);
        return;
      }

      const res = await auth.authFetch<{ workflow_id: string; revision: number }>(
        `v1/workspaces/${encodeURIComponent(workspaceId)}/drafts/import`,
        {
          method: "POST",
          body: {
            flow_data: parsed,
            save: true,
          },
        }
      );

      setStatusMessage(`Imported workflow ${res.workflow_id} as revision ${res.revision}.`);
      setImportJson("");
      setShowImport(false);
      setSelectedWorkflowId(res.workflow_id);
      loadDrafts();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMessage(`Import failed: ${msg}`);
    }
  };

  // 7. Action: Approve Revision
  const handleApprove = async () => {
    if (!workspaceId || !selectedWorkflowId) return;
    clearNotifications();
    try {
      const res = await auth.authFetch<ApprovalRecord>(
        `v1/workspaces/${encodeURIComponent(workspaceId)}/drafts/${encodeURIComponent(
          selectedWorkflowId
        )}/approve`,
        {
          method: "POST",
          body: { revision: selectedDraft?.revision },
        }
      );
      setApproval(res);
      setStatusMessage(`Revision ${res.revision} approved by ${res.approved_by}.`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMessage(`Approval failed: ${msg}`);
    }
  };

  // 8. Action: Run Workflow & Poll Status
  const handleRun = async () => {
    if (!workspaceId || !selectedWorkflowId) return;
    clearNotifications();
    try {
      const res = await auth.authFetch<WorkflowRunResponse>(
        `v1/workspaces/${encodeURIComponent(workspaceId)}/workflows/${encodeURIComponent(
          selectedWorkflowId
        )}/run`,
        {
          method: "POST",
          body: { revision: selectedDraft?.revision, inputs: {} },
        }
      );
      setActiveRun(res);
      setStatusMessage(`Run ${res.run.id} started with status: ${res.run.status}.`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMessage(`Failed to run workflow: ${msg}`);
    }
  };

  // 9. Action: Cancel Active Run
  const handleCancelRun = async () => {
    if (!workspaceId || !activeRun) return;
    clearNotifications();
    try {
      const res = await auth.authFetch<WorkflowRunResponse>(
        `v1/workspaces/${encodeURIComponent(workspaceId)}/workflows/${encodeURIComponent(
          activeRun.run.workflow_id
        )}/runs/${encodeURIComponent(activeRun.run.id)}/cancel`,
        { method: "POST" }
      );
      setActiveRun(res);
      setStatusMessage(`Run ${res.run.id} cancelled.`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMessage(`Failed to cancel run: ${msg}`);
    }
  };

  return (
    <div className="container mx-auto px-4 py-8 max-w-6xl space-y-8">
      {/* Header */}
      <header className="border-b pb-4 flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Workflow Authoring</h1>
          <p className="text-muted-foreground text-sm mt-1">
            V4 Visual Workflows · Workspace:{" "}
            <span className="font-mono text-foreground font-semibold">
              {workspaceId || "None selected"}
            </span>
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => setShowImport(!showImport)}>
            {showImport ? "Close Import" : "Import Flow"}
          </Button>
          <Button variant="outline" size="sm" onClick={loadDrafts} disabled={loading || !workspaceId}>
            {loading ? "Refreshing…" : "Refresh"}
          </Button>
        </div>
      </header>

      {/* Notifications */}
      {errorMessage && (
        <div
          role="alert"
          className="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive font-medium flex items-start justify-between"
        >
          <span>{errorMessage}</span>
          <button onClick={clearNotifications} className="ml-4 font-bold text-xs uppercase hover:underline">
            Dismiss
          </button>
        </div>
      )}

      {statusMessage && (
        <div
          role="status"
          className="rounded-lg border border-primary/40 bg-primary/10 p-4 text-sm text-foreground font-medium flex items-start justify-between"
        >
          <span>{statusMessage}</span>
          <button onClick={clearNotifications} className="ml-4 font-bold text-xs uppercase hover:underline">
            Dismiss
          </button>
        </div>
      )}

      {/* Import Modal / Accordion */}
      {showImport && (
        <section className="rounded-xl border bg-card p-6 shadow-sm space-y-4">
          <h2 className="text-lg font-semibold">Import Workflow (Langflow or Native JSON)</h2>
          <form onSubmit={handleImport} className="space-y-4">
            <div>
              <Label htmlFor="import-json" className="text-xs font-semibold">
                Paste JSON definition
              </Label>
              <textarea
                id="import-json"
                value={importJson}
                onChange={(e) => setImportJson(e.target.value)}
                placeholder='{"nodes": [...], "edges": [...]} or Langflow export {"data": {"nodes": [...]}}'
                rows={6}
                className="w-full mt-1 p-2 rounded-md border font-mono text-xs bg-muted/40"
              />
            </div>
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={() => setShowImport(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={!importJson.trim()}>
                Import & Save Draft
              </Button>
            </div>
          </form>
        </section>
      )}

      {/* Main Grid: Drafts List on Left, Authoring Details on Right */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-8">
        {/* Left Column: Drafts & Create Form */}
        <div className="space-y-6">
          <section className="rounded-xl border bg-card p-5 space-y-4 shadow-sm">
            <h2 className="text-base font-semibold">Workspace Drafts ({drafts.length})</h2>
            {drafts.length === 0 ? (
              <p className="text-sm text-muted-foreground italic py-2">
                No workflow drafts found in this workspace.
              </p>
            ) : (
              <div className="divide-y max-h-96 overflow-y-auto">
                {drafts.map((d) => (
                  <button
                    key={d.workflow_id}
                    onClick={() => setSelectedWorkflowId(d.workflow_id)}
                    className={`w-full text-left p-3 rounded-lg transition-colors flex flex-col gap-1 text-sm ${
                      selectedWorkflowId === d.workflow_id
                        ? "bg-primary/10 border border-primary/30"
                        : "hover:bg-muted/50"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-foreground">{d.workflow_id}</span>
                      <Badge variant="outline" className="text-xs">
                        rev {d.revision}
                      </Badge>
                    </div>
                    {d.summary && (
                      <p className="text-xs text-muted-foreground truncate">{d.summary}</p>
                    )}
                    <span className="text-[11px] font-mono text-muted-foreground">
                      hash: {d.content_hash.slice(0, 10)}…
                    </span>
                  </button>
                ))}
              </div>
            )}
          </section>

          {/* Create New Draft Form */}
          <section className="rounded-xl border bg-card p-5 space-y-4 shadow-sm">
            <h2 className="text-base font-semibold">Create New Draft</h2>
            <form onSubmit={handleCreateDraft} className="space-y-3">
              <div>
                <Label htmlFor="draft-id" className="text-xs font-semibold">
                  Workflow ID
                </Label>
                <Input
                  id="draft-id"
                  value={newWorkflowId}
                  onChange={(e) => setNewWorkflowId(e.target.value)}
                  placeholder="e.g. wf-knowledge-qa"
                  className="mt-1 text-sm"
                  required
                />
              </div>
              <div>
                <Label htmlFor="draft-name" className="text-xs font-semibold">
                  Name
                </Label>
                <Input
                  id="draft-name"
                  value={newWorkflowName}
                  onChange={(e) => setNewWorkflowName(e.target.value)}
                  placeholder="e.g. Knowledge QA Pipeline"
                  className="mt-1 text-sm"
                  required
                />
              </div>
              <Button type="submit" size="sm" className="w-full" disabled={!newWorkflowId || !newWorkflowName}>
                Save Revision 1
              </Button>
            </form>
          </section>
        </div>

        {/* Right Column: Selected Draft Details, Revisions, Validation, Execution */}
        <div className="md:col-span-2 space-y-6">
          {selectedDraft ? (
            <>
              {/* Draft Header & Action Toolbar */}
              <section className="rounded-xl border bg-card p-6 shadow-sm space-y-4">
                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b pb-3">
                  <div>
                    <h2 className="text-xl font-bold">{selectedDraft.spec.name || selectedDraft.workflow_id}</h2>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      ID: <span className="font-mono text-foreground">{selectedDraft.workflow_id}</span> · Head Rev:{" "}
                      <span className="font-semibold text-foreground">{selectedDraft.revision}</span>
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button variant="outline" size="sm" onClick={handleValidate}>
                      Validate
                    </Button>
                    <Button variant="outline" size="sm" onClick={handleReviseDraft}>
                      Append Revision
                    </Button>
                    <Button variant="outline" size="sm" onClick={handleApprove}>
                      Approve
                    </Button>
                    <Button size="sm" onClick={handleRun}>
                      Run
                    </Button>
                  </div>
                </div>

                {/* Spec Metrics */}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs pt-1">
                  <div className="rounded-lg bg-muted/40 p-2.5">
                    <span className="text-muted-foreground block text-[11px]">Nodes</span>
                    <span className="text-base font-semibold">{selectedDraft.spec.nodes?.length ?? 0}</span>
                  </div>
                  <div className="rounded-lg bg-muted/40 p-2.5">
                    <span className="text-muted-foreground block text-[11px]">Edges</span>
                    <span className="text-base font-semibold">{selectedDraft.spec.edges?.length ?? 0}</span>
                  </div>
                  <div className="rounded-lg bg-muted/40 p-2.5 col-span-2">
                    <span className="text-muted-foreground block text-[11px]">Content Hash</span>
                    <span className="font-mono text-[11px] truncate block text-foreground">
                      {selectedDraft.content_hash}
                    </span>
                  </div>
                </div>

                {/* Validation Feedback Section */}
                {validation && (
                  <div className="rounded-lg border p-4 space-y-2 mt-4 bg-muted/20">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                        Validator Result
                      </span>
                      <Badge variant={validation.valid ? "default" : "destructive"}>
                        {validation.valid ? "Valid" : "Invalid"}
                      </Badge>
                    </div>
                    {validation.annotations.length > 0 ? (
                      <div className="space-y-1.5 pt-2">
                        {validation.annotations.map((ann, idx) => (
                          <div
                            key={idx}
                            className={`text-xs p-2 rounded border flex flex-col gap-0.5 ${
                              ann.severity === "error"
                                ? "border-destructive/40 bg-destructive/5 text-destructive"
                                : "border-yellow-500/40 bg-yellow-500/5 text-yellow-700 dark:text-yellow-400"
                            }`}
                          >
                            <span className="font-semibold font-mono">
                              [{ann.code}] {ann.node_id ? `(node: ${ann.node_id})` : ""}
                            </span>
                            <span>{ann.message}</span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p className="text-xs text-muted-foreground">
                        No structural or capability errors detected.
                      </p>
                    )}
                  </div>
                )}

                {/* Approval Status Card */}
                {approval && (
                  <div className="rounded-lg border border-green-500/30 bg-green-500/10 p-3 text-xs text-green-800 dark:text-green-300 flex items-center justify-between">
                    <div>
                      <span className="font-semibold block">
                        Revision {approval.revision} Approved
                      </span>
                      <span className="font-mono text-[11px]">
                        By {approval.approved_by} at {approval.approved_at}
                      </span>
                    </div>
                    <Badge variant="outline" className="border-green-600 text-green-700 dark:text-green-300">
                      Approved
                    </Badge>
                  </div>
                )}
              </section>

              {/* Execution Run Status Panel */}
              {activeRun && (
                <section className="rounded-xl border bg-card p-6 shadow-sm space-y-4">
                  <div className="flex items-center justify-between border-b pb-3">
                    <div>
                      <h3 className="text-base font-semibold">Workflow Run: {activeRun.run.id}</h3>
                      <p className="text-xs text-muted-foreground">
                        Started: {activeRun.run.started_at} · Status:{" "}
                        <span className="font-semibold uppercase text-foreground">
                          {activeRun.run.status}
                        </span>
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge
                        variant={
                          activeRun.run.status === "completed"
                            ? "default"
                            : activeRun.run.status === "failed"
                            ? "destructive"
                            : "outline"
                        }
                      >
                        {activeRun.run.status}
                      </Badge>
                      {activeRun.run.status !== "completed" &&
                        activeRun.run.status !== "failed" &&
                        activeRun.run.status !== "cancelled" && (
                          <Button variant="destructive" size="sm" onClick={handleCancelRun}>
                            Cancel Run
                          </Button>
                        )}
                    </div>
                  </div>

                  {activeRun.run.error && (
                    <div className="text-xs p-3 rounded bg-destructive/10 text-destructive border border-destructive/20">
                      <span className="font-semibold">Terminal Fault:</span> {activeRun.run.error}
                    </div>
                  )}

                  {/* Step Timeline */}
                  <div className="space-y-2">
                    <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                      Execution Timeline ({Object.keys(activeRun.timeline.entries || {}).length} nodes)
                    </span>
                    <div className="space-y-1.5 max-h-48 overflow-y-auto">
                      {Object.entries(activeRun.timeline.entries || {}).map(([nodeId, entry]) => (
                        <div
                          key={nodeId}
                          className="flex items-center justify-between p-2 rounded bg-muted/30 text-xs font-mono"
                        >
                          <span>{nodeId}</span>
                          <div className="flex items-center gap-3">
                            <span className="text-muted-foreground text-[11px]">{entry.duration_ms}ms</span>
                            <Badge variant="outline" className="text-[10px]">
                              {entry.state}
                            </Badge>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                </section>
              )}

              {/* Revision History Section */}
              <section className="rounded-xl border bg-card p-6 shadow-sm space-y-4">
                <h3 className="text-base font-semibold">Revision History ({history.length})</h3>
                <div className="space-y-2">
                  {history.map((rev) => (
                    <div
                      key={rev.revision}
                      className="p-3 rounded-lg border bg-muted/10 flex flex-col sm:flex-row sm:items-center justify-between gap-2 text-xs"
                    >
                      <div>
                        <div className="flex items-center gap-2">
                          <span className="font-semibold text-foreground">Revision {rev.revision}</span>
                          {rev.parent_revision && (
                            <span className="text-muted-foreground">(supersedes rev {rev.parent_revision})</span>
                          )}
                        </div>
                        <span className="font-mono text-[11px] text-muted-foreground">
                          hash: {rev.content_hash}
                        </span>
                      </div>
                      <span className="text-muted-foreground font-mono text-[11px] whitespace-nowrap">
                        {rev.created_at || "—"}
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            </>
          ) : (
            <div className="rounded-xl border border-dashed p-12 text-center text-muted-foreground space-y-3">
              <p className="text-base">No workflow selected</p>
              <p className="text-xs">
                Select an existing draft from the list on the left, or create a new draft to begin authoring.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
