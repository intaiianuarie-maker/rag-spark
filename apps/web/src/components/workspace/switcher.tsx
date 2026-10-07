"use client";

import { useState } from "react";
import { Check, ChevronDown, Plus, RefreshCw, UserPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useWorkspaces } from "./workspace-provider";
import {
  acceptInvitation,
  createWorkspace,
  listMyInvitations,
  INVITATIONS_UNAVAILABLE,
  type Invitation,
} from "./contracts";
import { RoleBadge } from "./role-badge";

/**
 * Header workspace switcher (TASK-M1-002b). Renders nothing while
 * anonymous/loading; otherwise lists the caller's active memberships,
 * a create-workspace dialog, and (M1-001f) pending invitations with accept.
 */
export function WorkspaceSwitcher() {
  const { status, workspaces, activeId, active, setActive, reload, myRole } = useWorkspaces();
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [orgName, setOrgName] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [invitations, setInvitations] = useState<Invitation[] | null>(null);

  if (status === "anonymous" || status === "loading") return null;

  async function openCreate() {
    setCreateOpen(true);
    setFailure(null);
    setName("");
    setOrgName("");
  }

  async function handleCreate() {
    if (busy) return;
    setBusy(true);
    setFailure(null);
    try {
      const created = await createWorkspace({
        name: name.trim(),
        organization_name: orgName.trim() ? orgName.trim() : undefined,
      });
      setCreateOpen(false);
      setActive(created.id);
      await reload();
    } catch (error) {
      setFailure(describe(error));
    } finally {
      setBusy(false);
    }
  }

  async function handleAccept(workspaceId: string) {
    if (busy) return;
    setBusy(true);
    try {
      await acceptInvitation(workspaceId);
      setInvitations(null);
      await reload();
      setActive(workspaceId);
    } catch (error) {
      setFailure(describe(error));
    } finally {
      setBusy(false);
    }
  }

  async function loadInvitations() {
    if (invitations !== null) return;
    const result = await listMyInvitations();
    setInvitations(result === INVITATIONS_UNAVAILABLE ? [] : result);
  }

  const role = myRole(activeId);

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" size="sm" className="gap-2" aria-label="Switch workspace">
            <span className="max-w-32 truncate font-medium">{active?.name ?? "Workspaces"}</span>
            {role !== null && <RoleBadge role={role} />}
            <ChevronDown className="size-3.5 opacity-60" aria-hidden="true" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-72" onFocusCapture={loadInvitations}>
          <DropdownMenuLabel className="flex items-center justify-between">
            Workspaces
            <button
              type="button"
              onClick={() => void reload()}
              aria-label="Refresh workspace list"
              className="rounded p-1 hover:bg-neutral-100 dark:hover:bg-neutral-800"
            >
              <RefreshCw className="size-3.5 opacity-60" aria-hidden="true" />
            </button>
          </DropdownMenuLabel>
          {workspaces.length === 0 ? (
            <div className="px-2 py-3 text-xs text-neutral-500 dark:text-neutral-400">
              No workspaces yet — create the first one.
            </div>
          ) : (
            workspaces.map((ws) => (
              <DropdownMenuItem
                key={ws.id}
                onClick={() => setActive(ws.id)}
                className="flex items-center justify-between gap-2"
              >
                <span className="max-w-44 truncate">{ws.name}</span>
                {ws.id === activeId ? (
                  <Check className="size-3.5 opacity-60" aria-hidden="true" />
                ) : (
                  <RoleBadge role={ws.role} />
                )}
              </DropdownMenuItem>
            ))
          )}
          <DropdownMenuSeparator />
          <DropdownMenuItem onClick={openCreate} className="gap-2">
            <Plus className="size-3.5" aria-hidden="true" /> New workspace…
          </DropdownMenuItem>
          {invitations !== null && invitations.length > 0 && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuLabel>
                <span className="inline-flex items-center gap-1.5">
                  <UserPlus className="size-3.5" aria-hidden="true" /> Invitations
                </span>
              </DropdownMenuLabel>
              {invitations.map((invitation) => (
                <DropdownMenuItem
                  key={`${invitation.workspace_id}-${invitation.invited_at}`}
                  onClick={() => void handleAccept(invitation.workspace_id)}
                  className="flex items-center justify-between gap-2"
                >
                  <span className="max-w-44 truncate">{invitation.workspace_name}</span>
                  <span className="text-xs text-emerald-700 dark:text-emerald-300">
                    {busy ? "…" : "Accept"}
                  </span>
                </DropdownMenuItem>
              ))}
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>New workspace</DialogTitle>
            <DialogDescription>
              You become the owner. The organization is created for you.
            </DialogDescription>
          </DialogHeader>
          {failure !== null && (
            <div role="alert" className="rounded-lg border border-red-600/30 bg-red-50 p-2.5 text-sm text-red-900 dark:border-red-400/30 dark:bg-red-950/40 dark:text-red-200">
              {failure}
            </div>
          )}
          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="ws-name">Workspace name</Label>
              <Input
                id="ws-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Research team"
                disabled={busy}
                required
                autoFocus
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="ws-org">Organization name (optional)</Label>
              <Input
                id="ws-org"
                value={orgName}
                onChange={(e) => setOrgName(e.target.value)}
                placeholder="Defaults to the workspace name"
                disabled={busy}
              />
            </div>
          </div>
          <DialogFooter className="gap-2">
            <Button type="button" variant="outline" onClick={() => setCreateOpen(false)} disabled={busy}>
              Cancel
            </Button>
            <Button type="button" onClick={() => void handleCreate()} disabled={busy || name.trim().length === 0}>
              {busy ? "Creating…" : "Create workspace"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function describe(error: unknown): string {
  if (error instanceof Error && "code" in error) {
    const code = String((error as { code: unknown }).code);
    if (code === "already_member") return "That user is already a member.";
    if (code === "last_owner") return "A workspace must keep at least one owner.";
    if (code === "not_a_member") return "You do not have access to this workspace.";
  }
  if (error instanceof Error && error.message.includes("Network")) {
    return "Network error. Please try again.";
  }
  return "Something went wrong. Please try again.";
}
