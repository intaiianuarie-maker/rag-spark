"use client";

import { useEffect, useRef, useState } from "react";
import { KeyRound, Plus, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useWorkspaces } from "@/components/workspace/workspace-provider";
import { ConfirmDialog } from "@/components/workspace/confirm-dialog";
import { RoleBadge } from "@/components/workspace/role-badge";
import {
  ROLES,
  canManageKeys,
  issueApiKey,
  listApiKeys,
  revokeApiKey,
  type ApiKeyPublic,
  type Role,
} from "@/components/workspace/contracts";

/**
 * API keys management (TASK-M1-002b). Issue → secret shown exactly ONCE
 * (copy-once banner); list is prefix+metadata only; revoke with confirm.
 * The page itself needs owner/admin (upstream `key.manage` gate) — editors
 * and viewers see an honest permission state.
 */
export function KeysClient() {
  const { activeId, active, myRole, reload } = useWorkspaces();
  const [keys, setKeys] = useState<ApiKeyPublic[] | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "forbidden" | "error">("loading");
  const [issueOpen, setIssueOpen] = useState(false);
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [expiresAt, setExpiresAt] = useState("");
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ kind: "error" | "info"; text: string } | null>(null);
  const [createdSecret, setCreatedSecret] = useState<string | null>(null);
  const [createdName, setCreatedName] = useState<string>("");
  const [pendingRevoke, setPendingRevoke] = useState<ApiKeyPublic | null>(null);
  const inFlight = useRef(false);

  const callerRole = myRole(activeId);
  const allowed = canManageKeys(callerRole);

  async function loadKeys() {
    if (inFlight.current || activeId === null) return;
    inFlight.current = true;
    setState("loading");
    try {
      const result = await listApiKeys(activeId);
      setKeys(result);
      setState("ready");
    } catch (error: unknown) {
      const status = error instanceof Error && "status" in error ? (error as { status: number }).status : 0;
      setState(status === 403 ? "forbidden" : "error");
    } finally {
      inFlight.current = false;
    }
  }

  useEffect(() => {
    // Initial load in a scheduler callback (not in the effect body), keeping
    // state updates out of synchronous render (react-hooks/set-state-in-effect).
    const initialTimer = setTimeout(() => void loadKeys(), 0);
    return () => clearTimeout(initialTimer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  if (activeId === null) {
    return <PermissionState title="No workspace selected" text="Create or switch to a workspace first." />;
  }

  if (!allowed) {
    return (
      <PermissionState
        title="API keys need owner or admin"
        text={`Your role in ${active?.name ?? "this workspace"} is ${callerRole ?? "unknown"}. Ask an owner/admin to issue keys.`}
      />
    );
  }

  async function handleIssue() {
    if (busy || !activeId) return;
    setBusy(true);
    setBanner(null);
    try {
      const created = await issueApiKey(activeId, {
        name: name.trim(),
        role,
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
      });
      setIssueOpen(false);
      setName("");
      setExpiresAt("");
      setCreatedName(created.name);
      setCreatedSecret(created.key); // exactly-once display; cleared on dismiss
      await reload();
      await loadKeys();
    } catch (error) {
      setBanner({ kind: "error", text: describe(error) });
    } finally {
      setBusy(false);
    }
  }

  async function handleRevoke() {
    if (busy || !activeId || pendingRevoke === null) return;
    setBusy(true);
    setBanner(null);
    try {
      await revokeApiKey(activeId, pendingRevoke.id);
      setPendingRevoke(null);
      await loadKeys();
    } catch (error) {
      setBanner({ kind: "error", text: describe(error) });
      setPendingRevoke(null);
    } finally {
      setBusy(false);
    }
  }

  const activeKeys = (keys ?? []).filter((key) => key.revoked_at === null);
  const revokedKeys = (keys ?? []).filter((key) => key.revoked_at !== null);

  return (
    <section aria-label="API keys">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold">Keys</h2>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Refresh keys"
            onClick={() => void loadKeys()}
          >
            <RefreshCw className="size-3.5" aria-hidden="true" />
          </Button>
          <Button type="button" size="sm" className="gap-1.5" onClick={() => setIssueOpen(true)}>
            <Plus className="size-3.5" aria-hidden="true" /> Issue key
          </Button>
        </div>
      </div>

      {banner !== null && (
        <div
          role={banner.kind === "error" ? "alert" : "status"}
          className={`mb-4 rounded-lg border p-3 text-sm ${
            banner.kind === "error"
              ? "border-red-600/30 bg-red-50 text-red-900 dark:border-red-400/30 dark:bg-red-950/40 dark:text-red-200"
              : "border-emerald-600/30 bg-emerald-50 text-emerald-900 dark:border-emerald-400/30 dark:bg-emerald-950/40 dark:text-emerald-200"
          }`}
        >
          {banner.text}
        </div>
      )}

      {createdSecret !== null && (
        <div
          role="status"
          aria-label="New API key (shown once)"
          className="mb-4 rounded-xl border border-amber-600/40 bg-amber-50 p-4 dark:border-amber-400/40 dark:bg-amber-950/40"
        >
          <h3 className="text-sm font-semibold text-amber-900 dark:text-amber-200">
            {createdName} created — copy it now
          </h3>
          <p className="mt-1 text-xs text-amber-900/80 dark:text-amber-200/80">
            This secret is shown <strong>exactly once</strong> and is never stored
            or displayed again.
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <code className="break-all rounded bg-amber-100 px-2 py-1 font-mono text-xs text-amber-950 dark:bg-amber-900/60 dark:text-amber-100">
              {createdSecret}
            </code>
            <Button
              type="button"
              size="xs"
              onClick={() => {
                void navigator.clipboard?.writeText(createdSecret).catch(() => {});
                setBanner({ kind: "info", text: "Copied. Dismiss the banner when stored safely." });
              }}
            >
              Copy
            </Button>
            <Button type="button" size="xs" variant="outline" onClick={() => setCreatedSecret(null)}>
              I stored it — dismiss
            </Button>
          </div>
        </div>
      )}

      {state === "loading" && (
        <div aria-busy="true" className="space-y-2">
          <div className="h-8 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
          <div className="h-8 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
        </div>
      )}

      {state === "error" && (
        <div role="alert" className="rounded-xl border border-red-600/30 bg-red-50 p-4 text-sm text-red-900 dark:border-red-400/30 dark:bg-red-950/40 dark:text-red-200">
          Could not load keys. Try the refresh button.
        </div>
      )}

      {state === "ready" && (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Prefix</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {[...activeKeys, ...revokedKeys].map((key) => (
                <TableRow key={key.id} className={key.revoked_at !== null ? "opacity-60" : ""}>
                  <TableCell className="max-w-40 truncate">{key.name}</TableCell>
                  <TableCell className="font-mono text-xs">{key.prefix}</TableCell>
                  <TableCell>
                    <RoleBadge role={key.role} />
                  </TableCell>
                  <TableCell>
                    {key.revoked_at !== null ? (
                      <span className="text-xs text-red-700 dark:text-red-300">revoked</span>
                    ) : key.expires_at !== null && new Date(key.expires_at) <= new Date() ? (
                      <span className="text-xs text-amber-700 dark:text-amber-300">expired</span>
                    ) : (
                      <span className="text-xs text-emerald-700 dark:text-emerald-300">active</span>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    {key.revoked_at === null && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="xs"
                        onClick={() => setPendingRevoke(key)}
                      >
                        Revoke
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          {(keys ?? []).length === 0 && (
            <div role="status" className="mt-3 rounded-xl border border-dashed border-neutral-300 bg-white p-5 text-center dark:border-neutral-700 dark:bg-neutral-900">
              <span className="mx-auto flex h-10 w-10 items-center justify-center rounded-full border border-dashed border-neutral-400/60 text-neutral-500 dark:border-neutral-600 dark:text-neutral-400">
                <KeyRound className="h-5 w-5" aria-hidden="true" />
              </span>
              <h3 className="mt-3 text-sm font-semibold">No keys yet</h3>
              <p className="mx-auto mt-1 max-w-sm text-xs text-neutral-500 dark:text-neutral-400">
                Issue the first workspace key. The secret is shown once at creation.
              </p>
            </div>
          )}
        </>
      )}

      <Dialog open={issueOpen} onOpenChange={setIssueOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Issue a workspace key</DialogTitle>
            <DialogDescription>
              The secret is returned once, immediately after creation.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="key-name">Key name</Label>
              <Input
                id="key-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="pipeline-bot"
                disabled={busy}
                required
                autoFocus
              />
            </div>
            <div className="space-y-1.5">
              <Label>Role</Label>
              <Select value={role} onValueChange={(next) => setRole(next as Role)} disabled={busy}>
                <SelectTrigger className="w-40">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ROLES.filter((r) => r !== "owner").map((r) => (
                    <SelectItem key={r} value={r}>
                      {r === "admin" ? "Admin" : r === "editor" ? "Editor" : "Viewer"}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="key-expires">Expires at (optional)</Label>
              <Input
                id="key-expires"
                type="datetime-local"
                value={expiresAt}
                onChange={(e) => setExpiresAt(e.target.value)}
                disabled={busy}
              />
            </div>
          </div>
          <DialogFooter className="gap-2">
            <Button type="button" variant="outline" onClick={() => setIssueOpen(false)} disabled={busy}>
              Cancel
            </Button>
            <Button type="button" onClick={() => void handleIssue()} disabled={busy || name.trim().length === 0}>
              {busy ? "Issuing…" : "Issue key"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(open) => open === false && setPendingRevoke(null)}
        title="Revoke key?"
        description={`Bearer credentials for "${pendingRevoke?.name ?? ""}" stop working immediately.`}
        confirmLabel="Revoke"
        onConfirm={() => void handleRevoke()}
        busy={busy}
      />
    </section>
  );
}

function PermissionState({ title, text }: { title: string; text: string }) {
  return (
    <div role="status" className="rounded-xl border border-dashed border-neutral-300 bg-white p-6 text-center dark:border-neutral-700 dark:bg-neutral-900">
      <h3 className="text-sm font-semibold">{title}</h3>
      <p className="mx-auto mt-1 max-w-sm text-xs text-neutral-500 dark:text-neutral-400">{text}</p>
    </div>
  );
}

function describe(error: unknown): string {
  const code = error instanceof Error && "code" in error ? String((error as { code: unknown }).code) : "";
  if (code === "forbidden") return "Only owners/admins can perform this action.";
  if (code === "not_a_member") return "You do not have access to this workspace.";
  if (code === "key_not_found") return "That key no longer exists.";
  return "Something went wrong. Please try again.";
}
