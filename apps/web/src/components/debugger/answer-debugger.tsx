"use client";

import type { AnswerPayload, DebugTrace } from "@/app/ask/contracts";

export type AnswerDebuggerProps = {
  payload: AnswerPayload | null;
  showSourceText?: boolean;
};

function Absent({ label }: { label: string }) {
  return <p className="text-xs italic text-neutral-400">{label}: not reported</p>;
}

/**
 * Per-answer RAG debugger. Renders stages from the typed debug_trace structure.
 * Missing legs are disclosed explicitly (never invented). Source text is opt-in
 * (default off); the trace itself never carries source text.
 */
export function AnswerDebugger({ payload, showSourceText = false }: AnswerDebuggerProps) {
  if (!payload) return null;
  const t: DebugTrace | null | undefined = payload.debug_trace;

  return (
    <aside aria-label="Answer debugger" className="space-y-3 rounded-lg border p-4 text-sm">
      <h2 className="font-medium">Debugger</h2>

      {/* Query */}
      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400">Query</h3>
        {t?.query ? (
          <p className="text-xs">
            sha256: {t.query.sha256 ?? "—"} · characters: {t.query.characters ?? "—"}
          </p>
        ) : (
          <Absent label="query" />
        )}
      </section>

      {/* Rewrite */}
      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400">Rewrite</h3>
        {t?.rewrite ? (
          <p className="text-xs">status: {t.rewrite.status ?? "not_reported"}</p>
        ) : (
          <Absent label="rewrite" />
        )}
      </section>

      {/* Filters */}
      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400">Filters</h3>
        {t?.filters ? (
          <p className="text-xs">
            workspaces: {(t.filters.workspace_ids ?? []).join(", ") || "—"} ·
            include_public: {String(t.filters.include_public ?? "—")} ·
            mode: {t.filters.mode ?? "—"}
          </p>
        ) : (
          <Absent label="filters" />
        )}
      </section>

      {/* Retrieval */}
      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400">Retrieval</h3>
        {t?.retrieval ? (
          <>
            <p className="text-xs">
              status: {t.retrieval.status ?? "—"} · latency: {t.retrieval.latency_ms ?? "—"} ms ·
              scope: {t.retrieval.candidate_scope ?? "—"}
            </p>
            <ul className="list-disc pl-4 text-xs">
              {(t.retrieval.items ?? []).map((it, i) => (
                <li key={i}>
                  {it.chunk_id ?? "—"} ({it.document_id ?? "—"}) score={it.score ?? "—"}
                  {it.scores ? ` [${Object.entries(it.scores).map(([k, v]) => `${k}=${v}`).join(", ")}]` : ""}
                </li>
              ))}
            </ul>
          </>
        ) : (
          <Absent label="retrieval" />
        )}
      </section>

      {/* Context */}
      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400">Context</h3>
        {t?.context ? (
          <p className="text-xs">
            chunk_ids: {(t.context.chunk_ids ?? []).join(", ") || "—"} ·
            source_text_included: {String(t.context.source_text_included ?? false)}
          </p>
        ) : (
          <Absent label="context" />
        )}
      </section>

      {/* Generation */}
      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400">Generation</h3>
        {t?.generation ? (
          <p className="text-xs">
            status: {t.generation.status ?? "—"} · model: {t.generation.model ?? "—"} ·
            prompt_template_sha256: {t.generation.prompt_template_sha256 ?? "—"} ·
            latency: {t.generation.latency_ms ?? "—"} ms ·
            prompt_tokens: {t.generation.prompt_tokens ?? "null"} ·
            completion_tokens: {t.generation.completion_tokens ?? "null"} ·
            usage_status: {t.generation.usage_status ?? "—"}
          </p>
        ) : (
          <Absent label="generation" />
        )}
      </section>

      {/* Verification */}
      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400">Verification</h3>
        {t?.verification ? (
          <p className="text-xs">
            phase: {t.verification.phase ?? "—"} · status: {t.verification.status ?? "—"} ·
            latency: {t.verification.latency_ms ?? "—"} ms ·
            usage_status: {t.verification.usage_status ?? "—"}
          </p>
        ) : (
          <Absent label="verification" />
        )}
      </section>

      {/* Total */}
      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400">Total</h3>
        {t?.total_latency_ms != null ? (
          <p className="text-xs">total_latency_ms: {t.total_latency_ms}</p>
        ) : (
          <Absent label="total_latency_ms" />
        )}
      </section>

      {/* legacy AnswerPayload fields */}
      {payload.trace_id && (
        <p className="text-xs text-neutral-500">trace_id: {payload.trace_id}</p>
      )}
      {payload.verifier_model && (
        <p className="text-xs text-neutral-500">verifier_model: {payload.verifier_model}</p>
      )}
      {payload.limitations && payload.limitations.length > 0 && (
        <ul className="list-disc pl-4 text-xs text-neutral-500">
          {payload.limitations.map((l, i) => <li key={i}>{l}</li>)}
        </ul>
      )}
      <div className="text-xs">
        <span className="font-semibold">Sources (IDs only unless opted in):</span>
        <ul className="list-disc pl-4">
          {payload.citations.map((c, i) => (
            <li key={i}>
              {c.chunk_id}
              {showSourceText && (c as { content?: string | null }).content ? ` — ${(c as { content?: string | null }).content}` : ""}
            </li>
          ))}
        </ul>
      </div>
    </aside>
  );
}
