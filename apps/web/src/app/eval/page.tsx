"use client";

import { useWorkspaces } from "@/components/workspace/workspace-provider";
import { WorkspaceEval } from "./workspace-eval";

export default function EvalPage() {
  const { activeId } = useWorkspaces();
  if (!activeId) {
    return <main className="p-6"><p>Select a workspace first.</p></main>;
  }
  return <WorkspaceEval key={activeId} workspaceId={activeId} />;
}
