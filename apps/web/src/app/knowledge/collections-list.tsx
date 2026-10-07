"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { FolderOpen } from "lucide-react";
import { useWorkspaces } from "@/components/workspace/workspace-provider";
import { READS_UNAVAILABLE, listCollections, type CollectionPublic } from "./contracts";

/** Collections list (`/knowledge`) — first of the three browse levels. */
export function CollectionsList() {
  const { activeId, active, status } = useWorkspaces();
  const [collections, setCollections] = useState<CollectionPublic[] | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "unavailable" | "error">("loading");
  const inFlight = useRef(false);

  useEffect(() => {
    if (inFlight.current || activeId === null) return;
    inFlight.current = true;
    setState("loading");
    listCollections(activeId)
      .then((result) => {
        if (result === READS_UNAVAILABLE) {
          setCollections(null);
          setState("unavailable");
        } else {
          setCollections(result);
          setState("ready");
        }
      })
      .catch(() => setState("error"))
      .finally(() => {
        inFlight.current = false;
      });
  }, [activeId]);

  // Pre-hydration the provider has no workspaces yet (it loads client-side):
  // show the skeleton instead of a misleading "no workspace" empty state.
  if (status === "loading") {
    return (
      <div aria-busy="true" className="space-y-2">
        <div className="h-6 w-32 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
        <div className="h-20 animate-pulse rounded-xl bg-neutral-200 dark:bg-neutral-800" />
      </div>
    );
  }

  if (activeId === null) {
    return (
      <Empty
        title="No workspace selected"
        text="Create or switch to a workspace to browse its knowledge."
      />
    );
  }

  return (
    <section aria-label="Collections">
      <h1 className="text-xl font-semibold tracking-tight">Collections</h1>
      <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
        Browse knowledge in {active?.name ?? "this workspace"}.
      </p>

      <div className="mt-6">
        {state === "loading" && (
          <div aria-busy="true" className="grid gap-3 sm:grid-cols-2">
            <div className="h-20 animate-pulse rounded-xl bg-neutral-200 dark:bg-neutral-800" />
            <div className="h-20 animate-pulse rounded-xl bg-neutral-200 dark:bg-neutral-800" />
          </div>
        )}

        {state === "unavailable" && (
          <Empty
            title="Collections arrive with the read API"
            text="Uploads and ingestion already work; the collection list endpoint (TASK-M2-003) is not live yet."
          />
        )}

        {state === "error" && (
          <div
            role="alert"
            className="rounded-xl border border-red-600/30 bg-red-50 p-4 text-sm text-red-900 dark:border-red-400/30 dark:bg-red-950/40 dark:text-red-200"
          >
            Could not load collections. Refresh to retry.
          </div>
        )}

        {state === "ready" && (collections ?? []).length === 0 && (
          <Empty
            title="No collections yet"
            text="Collections group documents inside a workspace; the first one will appear here."
          />
        )}

        {state === "ready" && (collections ?? []).length > 0 && (
          <ul className="grid gap-3 sm:grid-cols-2">
            {collections!.map((collection) => (
              <li key={collection.id}>
                <Link
                  href={`/knowledge/${collection.id}`}
                  className="flex items-start gap-3 rounded-xl border border-neutral-200 bg-white p-4 transition-colors hover:border-neutral-400 dark:border-neutral-800 dark:bg-neutral-900 dark:hover:border-neutral-600"
                >
                  <FolderOpen
                    className="mt-0.5 size-4 shrink-0 opacity-60"
                    aria-hidden="true"
                  />
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{collection.name}</span>
                    {collection.description ? (
                      <span className="mt-0.5 block truncate text-xs text-neutral-600 dark:text-neutral-400">
                        {collection.description}
                      </span>
                    ) : null}
                    <span className="mt-1 block text-xs text-neutral-500 dark:text-neutral-400">
                      {collection.document_count ?? 0} document
                      {collection.document_count === 1 ? "" : "s"}
                    </span>
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

export function Empty({ title, text }: { title: string; text: string }) {
  return (
    <div
      role="status"
      className="rounded-xl border border-dashed border-neutral-300 bg-white p-8 text-center dark:border-neutral-700 dark:bg-neutral-900"
    >
      <h2 className="text-sm font-semibold">{title}</h2>
      <p className="mx-auto mt-1 max-w-sm text-xs text-neutral-500 dark:text-neutral-400">
        {text}
      </p>
    </div>
  );
}