"use client";

import { Badge } from "@/components/ui/badge";
import type { Role } from "./contracts";

const ROLE_STYLES: Record<Role, string> = {
  owner: "border-neutral-900/20 bg-neutral-900 text-white dark:border-white/20 dark:bg-white dark:text-neutral-900",
  admin: "border-neutral-600/20 bg-neutral-200 text-neutral-900 dark:bg-neutral-700 dark:text-white",
  editor: "border-emerald-600/30 bg-emerald-100 text-emerald-900 dark:bg-emerald-900/50 dark:text-emerald-200",
  viewer: "border-neutral-400/30 bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300",
};

const ROLE_LABELS: Record<Role, string> = {
  owner: "Owner",
  admin: "Admin",
  editor: "Editor", // canonical vocabulary: editor, never "member" (binding)
  viewer: "Viewer",
};

export function RoleBadge({ role }: { role: Role }) {
  return (
    <Badge variant="outline" className={`font-medium ${ROLE_STYLES[role]}`}>
      {ROLE_LABELS[role]}
    </Badge>
  );
}

export { ROLE_LABELS };
