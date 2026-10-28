"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, ChevronDown, ChevronRight } from "lucide-react";

import { auth } from "@/lib/auth-client";
import { Badge } from "@/components/ui/badge";
import { Empty } from "../../knowledge/collections-list";
import { useWorkspaces } from "@/components/workspace/workspace-provider";
import { buildReportSections } from "./report-sections";

/**
 * Research report view (T-V3-024) — evidence list + report with exact source clicks.
 *
 * Trace: V3 plan A4b. Depends on T-V3-022 (research report API) + T-V3-023.
 *
 * The report API (T-V3-022) serves the full honesty package: grounded sentences
 * with chunk provenance, named unknowns, and contradiction pairs with both
 * sides. This ticket renders it — nothing more.
 *
 * Reviewer checklist (from the card):
 * 1. Exact clicks: every grounded sentence and contradiction side renders its
 *    source chunk ids as clickable deep links to the exact
 *    document/version/chunk rather than a search result.
 * 2. Disputed distinct: contradictions render as TWO SIDES side-by-side —
 *    never merged into one smoothed "conflicting information" note.
 * 3. Unknowns shown: `unknowns` render as first-class sections, never hidden.
 *
 * Reasoning leak: nothing on this page renders model reasoning, chain-of-
 * thought, or intermediate steps. Every visible field is a typed wire field
 * from ResearchReportResponse. Same bar as T-V3-023's run pages.
 */

/* ------------------------------------------------------------------ *
 * Wire shapes — mirror of apps/api/app/agents/research.py (T-V3-022).
 * ------------------------------------------------------------------ */

export type ResearchReport = {
  research_id: string;
  workspace_id: string;
  status: string;
  topic: string;
  grounded_sentences: Array<{
    statement: string;
    chunk_ids: string[];
    document_id: string;
    chunk_id: string;
    confidence?: string;
    limitations?: string[];
    verdict?: string;
    disputed?: boolean;
  }>;
  unknowns: Array<{
    question: string;
    reason: string;
    statement?: string;
  }>;
  contradictions: Array<{
    question: string;
    claim_a: string;
    claim_b: string;
    chunk_ids_a: string[];
    chunk_ids_b: string[];
    evidence_a_id: string;
    evidence_b_id: string;
    reason: string;
  }>;
  contradiction_outline: string[];
  citations: string[];
  abstained: boolean;
  abstention_reason: string;
  searched: string[];
  limitations: string[];
  error_code?: string;
  missing?: string;
};

export type ResearchStatus = "loading" | "ready" | "unavailable" | "error";

export default function ResearchReportPage({ params }: { params: Promise<{ id: string }> }) {
  const { activeId } = useWorkspaces();
  const reqRef = useRef(0);
  const [researchId, setResearchId] = useState<string | null>(null);
  const [report, setReport] = useState<ResearchReport | null>(null);
  const [state, setState] = useState<ResearchStatus>("loading");
  const [openUnknowns, setOpenUnknowns] = useState(true);

  useEffect(() => {
    params.then((p) => setResearchId(p.id));
  }, [params]);

  const load = useCallback(async () => {
    if (activeId === null || researchId === null) return;
    const tok = ++reqRef.current;
    setState("loading");
    try {
      const result = await auth.authFetch<ResearchReport>(
        `/v1/workspaces/${activeId}/agent/research/${researchId}/report`,
        {},
      );
      if (reqRef.current !== tok) return;
      setReport(result);
      setState("ready");
    } catch (err) {
      if (reqRef.current !== tok) return;
      const status = (err as { status?: number })?.status;
      if (status === 404) {
        setState("unavailable");
      } else {
        setState("error");
      }
    }
  }, [activeId, researchId]);

  // Effect-sanctioned shape (mirrors app/agent-runs/page.tsx): the state
  // writes live inside a timer callback, not directly in the effect body.
  useEffect(() => {
    if (activeId === null || researchId === null) return;
    const timer = window.setTimeout(() => {
      void load();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [activeId, researchId, load]);

  if (researchId === null) {
    return <Empty title="Loading research run…" text="Resolving the run id." />;
  }
  if (state === "loading") {
    return (
      <section aria-busy="true" className="space-y-3">
        <div className="h-6 w-1/3 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
        <div className="h-4 w-2/3 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
      </section>
    );
  }
  if (state === "unavailable") {
    return (
      <Empty title="Research run not found" text="The run may have expired or the id is incorrect." />
    );
  }
  if (state === "error" || !report) {
    return (
      <div
        role="alert"
        className="rounded-xl border border-red-600/30 bg-red-50 p-4 text-sm text-red-900 dark:border-red-400/30 dark:bg-red-950/40 dark:text-red-200"
      >
        <div className="flex items-center gap-2">
          <AlertCircle className="size-4" aria-hidden="true" />
          Could not load the research report. Refresh to retry.
        </div>
      </div>
    );
  }

  if (report.error_code === "unavailable" || report.status === "unavailable") {
    return (
      <div
        role="alert"
        aria-label="Research pipeline unavailable"
        className="rounded-xl border border-amber-600/30 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-400/30 dark:bg-amber-950/40 dark:text-amber-200"
      >
        <div className="flex items-center gap-2 font-semibold">
          <AlertCircle className="size-4" aria-hidden="true" />
          Research pipeline unavailable
        </div>
        <p className="mt-1 text-xs">
          Research is unavailable in this workspace. No research pipeline is configured
          (missing: {report.missing || "research_pipeline"}). Nothing was searched,
          retrieved, generated, or cited.
        </p>
      </div>
    );
  }

  const s = buildReportSections(report as unknown as Record<string, unknown>);
  const disputedCount = s.evidence.filter((e) => e.disputed).length;

  return (
    <section aria-label="Research report">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Research report</h1>
          <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">{report.topic}</p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <Badge variant="outline" className="text-[10px]">
            status: {report.status}
          </Badge>
          <Badge variant="secondary" className="text-[10px]">
            {s.evidence.length - disputedCount} supported
          </Badge>
          <Badge variant="secondary" className="text-[10px]" data-testid="unknowns-count">
            {s.unknowns.length} unknown
          </Badge>
          <Badge variant="destructive" className="text-[10px]">
            {s.contradictions.length} disputed
          </Badge>
        </div>
      </div>

      {s.abstained && (
        <div className="mt-4 rounded-xl border border-amber-600/30 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-400/30 dark:bg-amber-950/40 dark:text-amber-200">
          This run abstained: {s.abstentionReason || "insufficient evidence"}
        </div>
      )}

      <div className="mt-6">
        <h2 className="text-sm font-semibold">Supported evidence</h2>
        {s.evidence.length ? (
          <ul className="mt-2 space-y-2" data-testid="evidence-list">
            {s.evidence.map((item, i: number) => (
              <li
                key={i}
                className={`rounded-xl border p-3 ${
                  item.disputed
                    ? "border-destructive/50 bg-destructive/5 dark:bg-destructive/10"
                    : "border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900"
                }`}
              >
                <div className="text-sm">{item.statement}</div>
                {item.confidence && (
                  <Badge variant="outline" className="mt-1 text-[10px]">
                    {item.confidence}
                  </Badge>
                )}
                {item.limitations.length > 0 && (
                  <div className="mt-1 text-[11px] text-neutral-500 dark:text-neutral-400">
                    {item.limitations.join("; ")}
                  </div>
                )}
                <div className="mt-2 flex flex-wrap gap-1">
                  {item.chunkLinks.length ? (
                    item.chunkLinks.map((href) => (
                      <Link
                        key={href}
                        href={href}
                        className="inline-flex items-center rounded bg-neutral-100 px-1.5 py-0.5 font-mono text-[11px] text-neutral-700 hover:bg-neutral-200 dark:bg-neutral-800 dark:text-neutral-300 dark:hover:bg-neutral-700"
                        title="Open exact chunk in document viewer"
                      >
                        {href.split("/").pop()?.slice(0, 8)}
                      </Link>
                    ))
                  ) : (
                    <span className="text-[11px] text-neutral-400">no chunk provenance</span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-neutral-500">No supported evidence yet.</p>
        )}
      </div>

      {s.contradictions.length > 0 && (
        <div className="mt-6">
          <h2 className="text-sm font-semibold">Disputed</h2>
          <div className="mt-2 space-y-3" data-testid="contradiction-list">
            {s.contradictions.map((c, i: number) => (
              <div
                key={i}
                className="rounded-xl border border-destructive/40 bg-destructive/5 p-3 dark:bg-destructive/10"
              >
                <div className="text-xs font-medium text-neutral-700 dark:text-neutral-300">
                  {c.question}
                </div>
                <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <div className="rounded border border-neutral-200 bg-white p-2 dark:border-neutral-700 dark:bg-neutral-900">
                    <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-neutral-500">
                      Side A
                    </div>
                    <div className="text-sm">{c.sideA.claim}</div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {c.sideA.chunkLinks.map((href) => (
                        <Link
                          key={href}
                          href={href}
                          className="inline-flex items-center rounded bg-neutral-100 px-1.5 py-0.5 font-mono text-[11px] text-neutral-700 hover:bg-neutral-200 dark:bg-neutral-800 dark:text-neutral-300 dark:hover:bg-neutral-700"
                        >
                          {href.split("/").pop()?.slice(0, 8)}
                        </Link>
                      ))}
                    </div>
                  </div>
                  <div className="rounded border border-neutral-200 bg-white p-2 dark:border-neutral-700 dark:bg-neutral-900">
                    <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-neutral-500">
                      Side B
                    </div>
                    <div className="text-sm">{c.sideB.claim}</div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {c.sideB.chunkLinks.map((href) => (
                        <Link
                          key={href}
                          href={href}
                          className="inline-flex items-center rounded bg-neutral-100 px-1.5 py-0.5 font-mono text-[11px] text-neutral-700 hover:bg-neutral-200 dark:bg-neutral-800 dark:text-neutral-300 dark:hover:bg-neutral-700"
                        >
                          {href.split("/").pop()?.slice(0, 8)}
                        </Link>
                      ))}
                    </div>
                  </div>
                </div>
                <div className="mt-1 text-[11px] text-neutral-500 dark:text-neutral-400">
                  reason: {c.reason}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="mt-6">
        <button
          type="button"
          onClick={() => setOpenUnknowns((v) => !v)}
          className="flex w-full items-center gap-1 text-sm font-semibold"
          aria-expanded={openUnknowns}
        >
          {openUnknowns ? (
            <ChevronDown className="size-4" aria-hidden="true" />
          ) : (
            <ChevronRight className="size-4" aria-hidden="true" />
          )}
          Unknowns
        </button>
        {openUnknowns && (
          <ul className="mt-2 space-y-1" data-testid="unknowns-list">
            {s.unknowns.map((u, i: number) => (
              <li
                key={i}
                className="rounded border border-amber-500/30 bg-amber-50 px-3 py-2 text-xs dark:border-amber-400/30 dark:bg-amber-950/30"
              >
                <span className="font-medium">{u.reason}</span>: {u.text}
              </li>
            ))}
          </ul>
        )}
      </div>

      {s.limitations.length > 0 && (
        <div className="mt-6 text-[11px] text-neutral-500 dark:text-neutral-400">
          <span className="font-medium">Limitations:</span> {s.limitations.join("; ")}
        </div>
      )}
    </section>
  );
}
