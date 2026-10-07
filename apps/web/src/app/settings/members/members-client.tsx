"use client";

import { useEffect, useRef, useState } from "react";
import { UserPlus, RefreshCw } from "lucide-react";
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
  ROSTER_UNAVAILABLE,
  ROLES,
  changeMemberRole,
  inviteMember,
  listMembers,
  removeMember,
  type MemberRow,
  type Role,
} from "@/components/workspace/contracts";

/**
 * Members management (TASK-M1-002b): roster (M1-001f), invite dialog,
 * admin role changes, remove/leave with confirms and empty states.
 */
export function MembersClient() {
  const { activeId, myRole, reload } = useWorkspaces();
  const [roster, setRoster] = useState<MemberRow[] | null>(null);
  const [rosterState, setRosterState] = useState<"loading" | "ready" | "unavailable" | "error">("loading");
  const [inviteOpen, setInviteOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ kind: "error" | "info"; text: string } | null>(null);
  const [pendingRemove, setPendingRemove] = useState<MemberRow | null>(null);
  const [pendingRole, setPendingRole] = useState<{ row: MemberRow; next: Role } | null>(null);
  const inFlight = useRef(false);

  const callerRole = myRole(activeId);
  const isAdmin = callerRole === "owner" || callerRole === "admin";

  async function loadRoster() {
    if (inFlight.current || activeId === null) return;
    inFlight.current = true;
    setRosterState("loading");
    try {
      const result = await listMembers(activeId);
      if (result === ROSTER_UNAVAILABLE) {
        setRoster(null);
        setRosterState("unavailable");
      } else {
        setRoster(result);
        setRosterState("ready");
      }
    } catch {
      setRosterState("error");
    } finally {
      inFlight.current = false;
    }
  }

  useEffect(() => {
    // Initial load in a scheduler callback (not in the effect body), keeping
    // state updates out of synchronous render (react-hooks/set-state-in-effect).
    const initialTimer = setTimeout(() => void loadRoster(), 0);
    return () => clearTimeout(initialTimer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  if (activeId === null) {
    return <EmptyState title="No workspace selected" description="Create or switch to a workspace to manage members." />;
  }

  async function handleInvite() {
    if (busy || !activeId) return;
    setBusy(true);
    setBanner(null);
    try {
      await inviteMember(activeId, { email: email.trim(), role });
      setInviteOpen(false);
      setEmail("");
      setBanner({ kind: "info", text: `Invite recorded for ${email.trim()} (role: ${role}).` });
      await reload();
      await loadRoster();
    } catch (error) {
      setBanner({ kind: "error", text: describe(error) });
    } finally {
      setBusy(false);
    }
  }

  async function handleRoleChange() {
    if (busy || !activeId || pendingRole === null) return;
    setBusy(true);
    setBanner(null);
    try {
      await changeMemberRole(activeId, pendingRole.row.user_id, pendingRole.next);
      setPendingRole(null);
      await loadRoster();
    } catch (error) {
      setBanner({ kind: "error", text: describe(error) });
    } finally {
      setBusy(false);
    }
  }

  async function handleRemove() {
    if (busy || !activeId || pendingRemove === null) return;
    setBusy(true);
    setBanner(null);
    try {
      await removeMember(activeId, pendingRemove.user_id);
      setPendingRemove(null);
      await reload();
      await loadRoster();
    } catch (error) {
      setBanner({ kind: "error", text: describe(error) });
      setPendingRemove(null);
    } finally {
      setBusy(false);
    }
  }

  const invitedRows = (roster ?? []).filter((row) => row.status === "invited");
  const activeRows = (roster ?? []).filter((row) => row.status === "active");

  return (
    <section aria-label="Members">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold">Roster</h2>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Refresh roster"
            onClick={() => void loadRoster()}
          >
            <RefreshCw className="size-3.5" aria-hidden="true" />
          </Button>
          {isAdmin && (
            <Button type="button" size="sm" className="gap-1.5" onClick={() => setInviteOpen(true)}>
              <UserPlus className="size-3.5" aria-hidden="true" /> Invite member
            </Button>
          )}
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

      {rosterState === "loading" && <SkeletonTable rows={3} />}

      {rosterState === "unavailable" && (
        <div role="status" className="rounded-xl border border-dashed border-neutral-300 bg-white p-5 text-center dark:border-neutral-700 dark:bg-neutral-900">
          <h3 className="text-sm font-semibold">Roster UI arrives with M1-001f</h3>
          <p className="mx-auto mt-1 max-w-sm text-xs text-neutral-500 dark:text-neutral-400">
            The backend member-list endpoint is not live yet. Invites and role
            changes are already recorded server-side; the roster will appear here
            once <code className="font-mono">GET /v1/workspaces/&#123;id&#125;/members</code> lands.
          </p>
          <p className="mt-2 text-xs text-neutral-500 dark:text-neutral-400">
            Your role here: <RoleBadge role={callerRole ?? "viewer"} />
          </p>
        </div>
      )}

      {rosterState === "error" && (
        <div role="alert" className="rounded-xl border border-red-600/30 bg-red-50 p-4 text-sm text-red-900 dark:border-red-400/30 dark:bg-red-950/40 dark:text-red-200">
          Could not load the roster. Try the refresh button.
        </div>
      )}

      {rosterState === "ready" && (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Email</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {activeRows.map((row) => (
                <TableRow key={row.user_id}>
                  <TableCell className="max-w-56 truncate">{row.email}</TableCell>
                  <TableCell>
                    {isAdmin && callerRole === "owner" ? (
                      <Select
                        value={row.role}
                        onValueChange={(next) => setPendingRole({ row, next: next as Role })}
                        disabled={busy}
                      >
                        <SelectTrigger size="sm" className="w-28">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {ROLES.map((r) => (
                            <SelectItem key={r} value={r}>
                              {r === "owner" ? "Owner" : r === "admin" ? "Admin" : r === "editor" ? "Editor" : "Viewer"}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : (
                      <RoleBadge role={row.role} />
                    )}
                  </TableCell>
                  <TableCell>Active</TableCell>
                  <TableCell className="text-right">
                    {isAdmin && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="xs"
                        onClick={() => setPendingRemove(row)}
                      >
                        Remove
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          {activeRows.length === 0 && (
            <p className="mt-3 text-xs text-neutral-500 dark:text-neutral-400">
              No active members listed yet.
            </p>
          )}

          {invitedRows.length > 0 && (
            <div className="mt-6">
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
                Pending invitations
              </h3>
              <ul className="space-y-1 text-sm">
                {invitedRows.map((row) => (
                  <li key={`${row.user_id}-${row.status}`} className="flex items-center gap-2">
                    <span className="truncate">{row.email}</span>
                    <RoleBadge role={row.role} />
                    <span className="text-xs text-amber-700 dark:text-amber-300">invited</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}

      <Dialog open={inviteOpen} onOpenChange={setInviteOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Invite a member</DialogTitle>
            <DialogDescription>
              The email must already have an account on this instance.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="invite-email">Email</Label>
              <Input
                id="invite-email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="teammate@example.com"
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
          </div>
          <DialogFooter className="gap-2">
            <Button type="button" variant="outline" onClick={() => setInviteOpen(false)} disabled={busy}>
              Cancel
            </Button>
            <Button type="button" onClick={() => void handleInvite()} disabled={busy || email.trim().length === 0}>
              {busy ? "Inviting…" : "Send invite"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={pendingRemove !== null}
        onOpenChange={(open) => open === false && setPendingRemove(null)}
        title="Remove member?"
        description={`${pendingRemove?.email ?? ""} loses access to this workspace immediately.`}
        confirmLabel="Remove"
        onConfirm={() => void handleRemove()}
        busy={busy}
      />

      <ConfirmDialog
        open={pendingRole !== null}
        onOpenChange={(open) => open === false && setPendingRole(null)}
        title="Change role?"
        description={`Set ${pendingRole?.row.email ?? ""} to ${pendingRole?.next ?? ""}?`}
        confirmLabel="Set role"
        onConfirm={() => void handleRoleChange()}
        busy={busy}
      />
    </section>
  );
}

export function EmptyState({ title, description }: { title: string; description?: string }) {
  return (
    <div role="status" className="rounded-xl border border-dashed border-neutral-300 bg-white p-6 text-center dark:border-neutral-700 dark:bg-neutral-900">
      <h3 className="text-sm font-semibold">{title}</h3>
      {description && (
        <p className="mx-auto mt-1 max-w-sm text-xs text-neutral-500 dark:text-neutral-400">
          {description}
        </p>
      )}
    </div>
  );
}

function SkeletonTable({ rows }: { rows: number }) {
  return (
    <div aria-busy="true" className="space-y-2">
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="h-8 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
      ))}
    </div>
  );
}

function describe(error: unknown): string {
  const code = error instanceof Error && "code" in error ? String((error as { code: unknown }).code) : "";
  if (code === "already_member") return "That user is already a member.";
  if (code === "last_owner") return "A workspace must keep at least one owner.";
  if (code === "user_not_found") return "No account exists for that email on this instance.";
  if (code === "member_not_found") return "That member no longer exists.";
  if (code === "not_a_member") return "You do not have access to this workspace.";
  if (code === "forbidden") return "Only owners/admins can perform this action.";
  return "Something went wrong. Please try again.";
}
