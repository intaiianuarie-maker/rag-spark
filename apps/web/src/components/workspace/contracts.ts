"use client";

/**
 * Workspace/membership/keys contracts + typed API client functions
 * (TASK-M1-002b). Shapes are the frozen backend contract:
 *  - M1-001b/c endpoints: /v1/workspaces*, roles owner/admin/editor/viewer.
 *  - M1-001f (boss-sanctioned, implemented by code1 after M2-001a):
 *      GET /v1/workspaces/{id}/members  -> MemberRow[] (invitees included)
 *      GET /v1/me/invitations           -> Invitation[] (caller-filtered)
 *    Until it lands, those two calls return `rosterUnavailable` /
 *    `invitationsUnavailable` sentinels instead of throwing.
 */

import { auth, AuthApiError, AuthLoginRequiredError, type AuthFetchInit } from "@/lib/auth-client";

export type Role = "owner" | "admin" | "editor" | "viewer";
export const ROLES: readonly Role[] = ["owner", "admin", "editor", "viewer"];

export type WorkspacePublic = {
  id: string;
  name: string;
  organization_id: string;
  role: Role;
  created_at: string;
};

export type MemberStatus = "invited" | "active";

/** M1-001f contract (stubbed until it lands — see `listMembers`). */
export type MemberRow = {
  user_id: string;
  email: string;
  role: Role;
  status: MemberStatus;
};

/** M1-001f contract (stubbed until it lands — see `listMyInvitations`). */
export type Invitation = {
  workspace_id: string;
  workspace_name: string;
  role: Role;
  invited_at: string;
};

export type ApiKeyPublic = {
  id: string;
  name: string;
  prefix: string;
  role: Role;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
};

export type ApiKeyCreated = ApiKeyPublic & { key: string };

/** Sentinel results for the two M1-001f calls before the backend lands. */
export const ROSTER_UNAVAILABLE = "rosterUnavailable" as const;
export const INVITATIONS_UNAVAILABLE = "invitationsUnavailable" as const;

function joined(path: string): string {
  return path.replace(/^\/+/, "");
}

async function jsonCall<T>(
  path: string,
  init: AuthFetchInit,
): Promise<T> {
  return auth.authFetch<T>(joined(path), init);
}

function isUnavailability(error: unknown): boolean {
  return (
    error instanceof AuthApiError &&
    (error.status === 404 || error.status === 405)
  );
}

// ---- workspaces ---------------------------------------------------------------

export async function listMyWorkspaces(): Promise<WorkspacePublic[]> {
  return jsonCall<WorkspacePublic[]>("/v1/workspaces", { authMode: "strict" });
}

export async function createWorkspace(input: {
  name: string;
  organization_name?: string;
}): Promise<WorkspacePublic> {
  return jsonCall<WorkspacePublic>("/v1/workspaces", {
    method: "POST",
    body: input,
  });
}

export async function renameWorkspace(
  workspaceId: string,
  name: string,
): Promise<WorkspacePublic> {
  return jsonCall<WorkspacePublic>(`/v1/workspaces/${workspaceId}`, {
    method: "PATCH",
    body: { name },
  });
}

// ---- members (M1-001f) ----------------------------------------------------------

/** Roster: invitees included. `rosterUnavailable` until M1-001f lands. */
export async function listMembers(
  workspaceId: string,
): Promise<MemberRow[] | typeof ROSTER_UNAVAILABLE> {
  try {
    return await jsonCall<MemberRow[]>(`/v1/workspaces/${workspaceId}/members`, {
      authMode: "strict",
    });
  } catch (error) {
    if (isUnavailability(error)) return ROSTER_UNAVAILABLE;
    throw error;
  }
}

/** Owner/admin invite by account email. 404 `user_not_found` when absent. */
export async function inviteMember(
  workspaceId: string,
  input: { email: string; role: Role },
): Promise<MemberRow> {
  return jsonCall<MemberRow>(`/v1/workspaces/${workspaceId}/members`, {
    method: "POST",
    body: input,
  });
}

/** The invited caller accepts their own invitation (idempotent upstream). */
export async function acceptInvitation(workspaceId: string): Promise<void> {
  await jsonCall<void>(`/v1/workspaces/${workspaceId}/members/accept`, {
    method: "POST",
    body: {},
  });
}

/** Owner/admin change role; last-owner guard raises 409 `last_owner`. */
export async function changeMemberRole(
  workspaceId: string,
  userId: string,
  role: Role,
): Promise<MemberRow> {
  return jsonCall<MemberRow>(`/v1/workspaces/${workspaceId}/members/${userId}`, {
    method: "PATCH",
    body: { role },
  });
}

/** Owner/admin remove, or self-leave (caller == userId). 204. */
export async function removeMember(workspaceId: string, userId: string): Promise<void> {
  await jsonCall<void>(`/v1/workspaces/${workspaceId}/members/${userId}`, {
    method: "DELETE",
    body: undefined,
  });
}

/** Caller-filtered pending invitations. Unavailable until M1-001f lands. */
export async function listMyInvitations():
  Promise<Invitation[] | typeof INVITATIONS_UNAVAILABLE> {
  try {
    return await jsonCall<Invitation[]>("/v1/me/invitations", { authMode: "strict" });
  } catch (error) {
    if (isUnavailability(error)) return INVITATIONS_UNAVAILABLE;
    throw error;
  }
}

// ---- API keys -------------------------------------------------------------------

/** Issue a workspace key; the secret comes back exactly once (copy-once). */
export async function issueApiKey(
  workspaceId: string,
  input: { name: string; role: Role; expires_at?: string | null },
): Promise<ApiKeyCreated> {
  return jsonCall<ApiKeyCreated>(`/v1/workspaces/${workspaceId}/keys`, {
    method: "POST",
    body: input,
  });
}

/** List workspace keys (prefix + metadata only). Requires owner/admin. */
export async function listApiKeys(workspaceId: string): Promise<ApiKeyPublic[]> {
  return jsonCall<ApiKeyPublic[]>(`/v1/workspaces/${workspaceId}/keys`, {
    authMode: "strict",
  });
}

export async function revokeApiKey(workspaceId: string, keyId: string): Promise<void> {
  await jsonCall<void>(`/v1/workspaces/${workspaceId}/keys/${keyId}`, {
    method: "DELETE",
    body: undefined,
  });
}

/** Does the caller's role permit key management (owner/admin)? */
export function canManageKeys(role: Role | null | undefined): boolean {
  return role === "owner" || role === "admin";
}

export { AuthApiError, AuthLoginRequiredError };
