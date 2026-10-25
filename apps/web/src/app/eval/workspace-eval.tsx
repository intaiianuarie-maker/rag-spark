"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  listEvalDatasets,
  listEvalRuns,
  createEvalDataset,
  runEval,
  replayEvalRun,
  compareEvalRuns,
  type EvalDataset,
  type EvalRun,
  type EvalExample,
} from "./contracts";

export function WorkspaceEval({ workspaceId }: { workspaceId: string }) {
  const [name, setName] = useState("");
  const [question, setQuestion] = useState("");
  const [expectedAnswer, setExpectedAnswer] = useState("");
  const [expectedSources, setExpectedSources] = useState("");
  const [importJsonl, setImportJsonl] = useState("");
  const [mode, setMode] = useState<"hybrid" | "semantic" | "keyword">("hybrid");
  const [topK, setTopK] = useState(10);
  const [showSourceText, setShowSourceText] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [cmpBaseline, setCmpBaseline] = useState("");
  const [cmpCandidate, setCmpCandidate] = useState("");
  const [cmpDimension, setCmpDimension] = useState("mode");

  const [datasets, setDatasets] = useState<EvalDataset[]>([]);
  const [runs, setRuns] = useState<EvalRun[]>([]);
  const [selectedRun, setSelectedRun] = useState<EvalRun | null>(null);
  const [cmpResult, setCmpResult] = useState<string | null>(null);

  const aliveRef = useRef(true);
  const busyRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const requestEpochRef = useRef(0);

  const startBusy = useCallback(() => {
    if (busyRef.current) return false;
    busyRef.current = true;
    setBusy(true);
    return true;
  }, []);

  const endBusy = useCallback(() => {
    busyRef.current = false;
    if (aliveRef.current) setBusy(false);
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      requestEpochRef.current += 1;
    };
  }, []);

  const refresh = useCallback(async (preserveSelection = true) => {
    const ws = workspaceId;
    const epoch = ++requestEpochRef.current;
    try {
      const [ds, rs] = await Promise.all([listEvalDatasets(ws), listEvalRuns(ws)]);
      if (!aliveRef.current) return;
      if (epoch !== requestEpochRef.current) return;
      setDatasets(ds);
      setRuns(rs);
      // Preserve selectedRun/cmpResult when same scope; update selected run
      // from the refreshed list if present.
      setSelectedRun((prev) => {
        if (!preserveSelection || !prev) return prev;
        const found = rs.find((r) => r.run_id === prev.run_id);
        return found ?? prev;
      });
    } catch (e) {
      if (!aliveRef.current) return;
      if (epoch !== requestEpochRef.current) return;
      setStatus(e instanceof Error ? e.message : "load failed");
    }
  }, [workspaceId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh(false);
  }, [refresh]);

  const handleCreateDataset = useCallback(async () => {
    if (busyRef.current) return;
    if (!name.trim()) return;
    if (!startBusy()) return;
    let examples: EvalExample[] = [];
    const trimmedImport = importJsonl.trim();
    try {
      if (trimmedImport.length > 0) {
        examples = trimmedImport
          .split(/\n+/)
          .map((l) => l.trim())
          .filter((l) => l.length > 0)
          .map((line) => {
            const obj = JSON.parse(line) as Record<string, unknown>;
            if (typeof obj.question !== "string" || obj.question.trim().length === 0) {
              throw new Error("each example requires a non-empty question");
            }
            return {
              question: obj.question.trim(),
              expected_answer: typeof obj.expected_answer === "string" ? obj.expected_answer : "",
              expected_sources: Array.isArray(obj.expected_sources)
                ? obj.expected_sources.filter((x): x is string => typeof x === "string")
                : [],
              tags: Array.isArray(obj.tags) ? obj.tags.filter((x): x is string => typeof x === "string") : [],
            };
          });
      } else {
        if (!question.trim()) { endBusy(); return; }
        examples = [
          {
            question: question.trim(),
            expected_answer: expectedAnswer.trim() || "",
            expected_sources: expectedSources
              .split(",")
              .map((s) => s.trim())
              .filter((s) => s.length > 0),
            tags: [],
          },
        ];
      }
      if (examples.length === 0) {
        setStatus("no examples provided");
        endBusy();
        return;
      }
      const epoch = ++requestEpochRef.current;
      await createEvalDataset(workspaceId, name.trim(), examples);
      if (!aliveRef.current) return;
      if (epoch !== requestEpochRef.current) return;
      setName("");
      setQuestion("");
      setExpectedAnswer("");
      setExpectedSources("");
      setImportJsonl("");
      await refresh(true);
    } catch (e) {
      if (!aliveRef.current) return;
      setStatus(e instanceof Error ? e.message : "create failed");
    } finally {
      if (aliveRef.current) endBusy();
    }
  }, [workspaceId, name, question, expectedAnswer, expectedSources, importJsonl, refresh, startBusy, endBusy]);

  const handleRun = useCallback(async (datasetId?: string) => {
    if (busyRef.current) return;
    if (!startBusy()) return;
    const epoch = ++requestEpochRef.current;
    try {
      const run = await runEval(workspaceId, {
        dataset_id: datasetId,
        config: { mode, top_k: topK },
      });
      if (!aliveRef.current) return;
      if (epoch !== requestEpochRef.current) return;
      setSelectedRun(run);
      await refresh(true);
    } catch (e) {
      if (!aliveRef.current) return;
      if (epoch !== requestEpochRef.current) return;
      setStatus(e instanceof Error ? e.message : "run failed");
    } finally {
      if (aliveRef.current) endBusy();
    }
  }, [workspaceId, mode, topK, refresh, startBusy, endBusy]);

  const handleReplay = useCallback(async (runId: string) => {
    if (busyRef.current) return;
    if (!startBusy()) return;
    const epoch = ++requestEpochRef.current;
    try {
      const run = await replayEvalRun(workspaceId, runId);
      if (!aliveRef.current) return;
      if (epoch !== requestEpochRef.current) return;
      setSelectedRun(run);
      await refresh(true);
    } catch (e) {
      if (!aliveRef.current) return;
      if (epoch !== requestEpochRef.current) return;
      setStatus(e instanceof Error ? e.message : "replay failed");
    } finally {
      if (aliveRef.current) endBusy();
    }
  }, [workspaceId, refresh, startBusy, endBusy]);

  const handleCompare = useCallback(async () => {
    if (busyRef.current) return;
    if (!cmpBaseline || !cmpCandidate) return;
    if (!startBusy()) return;
    const epoch = ++requestEpochRef.current;
    try {
      const res = await compareEvalRuns(workspaceId, cmpBaseline, cmpCandidate, cmpDimension);
      if (!aliveRef.current) return;
      if (epoch !== requestEpochRef.current) return;
      setCmpResult(JSON.stringify(res, null, 2));
    } catch (e) {
      if (!aliveRef.current) return;
      if (epoch !== requestEpochRef.current) return;
      setStatus(e instanceof Error ? e.message : "compare failed");
    } finally {
      if (aliveRef.current) endBusy();
    }
  }, [workspaceId, cmpBaseline, cmpCandidate, cmpDimension, startBusy, endBusy]);

  return (
    <main className="mx-auto max-w-5xl space-y-6 p-6">
      <h1 className="text-xl font-semibold">Evaluation</h1>
      {status && <p role="alert" className="text-sm text-red-600">{status}</p>}
      {busy && <p role="status" aria-live="polite" className="text-sm text-neutral-500">Working…</p>}

      <section aria-label="Create dataset" className="space-y-2 rounded-lg border p-4">
        <h2 className="font-medium">Create dataset</h2>
        <input className="w-full rounded border px-2 py-1" aria-label="Dataset name" placeholder="name" disabled={busy} value={name} onChange={(e) => setName(e.target.value)} />
        <input className="w-full rounded border px-2 py-1" aria-label="Question" placeholder="question" disabled={busy} value={question} onChange={(e) => setQuestion(e.target.value)} />
        <input className="w-full rounded border px-2 py-1" aria-label="Expected answer" placeholder="expected answer (optional)" disabled={busy} value={expectedAnswer} onChange={(e) => setExpectedAnswer(e.target.value)} />
        <input className="w-full rounded border px-2 py-1" aria-label="Expected source ids" placeholder="expected source ids (comma-separated)" disabled={busy} value={expectedSources} onChange={(e) => setExpectedSources(e.target.value)} />
        <textarea className="w-full rounded border px-2 py-1 font-mono text-xs" aria-label="Import examples JSONL" disabled={busy} rows={4} placeholder='{"question":"...","expected_answer":"...","expected_sources":["doc-1"],"tags":["EN"]}' value={importJsonl} onChange={(e) => setImportJsonl(e.target.value)} />
        <button type="button" onClick={handleCreateDataset} disabled={busy} aria-disabled={busy} className="rounded bg-blue-600 px-3 py-1 text-white disabled:opacity-50">Create</button>
      </section>

      <section aria-label="Run eval" className="space-y-2 rounded-lg border p-4">
        <h2 className="font-medium">Run eval</h2>
        <div className="flex gap-2">
          <select aria-label="Search mode" className="rounded border px-2 py-1" disabled={busy} value={mode} onChange={(e) => setMode(e.target.value as "hybrid" | "semantic" | "keyword")}>
            <option value="hybrid">hybrid</option>
            <option value="semantic">semantic</option>
            <option value="keyword">keyword</option>
          </select>
          <input type="number" min={1} max={100} className="w-20 rounded border px-2 py-1" aria-label="Top K" disabled={busy} value={topK} onChange={(e) => setTopK(Number(e.target.value))} />
        </div>
        <div className="flex flex-wrap gap-2">
          {datasets.map((d) => (
            <button key={d.id} type="button" onClick={() => handleRun(d.id)} className="rounded border px-2 py-1 text-sm" disabled={busy}>
              Run {d.name}
            </button>
          ))}
        </div>
      </section>

      <section aria-label="Datasets" className="space-y-1">
        <h2 className="font-medium">Datasets ({datasets.length})</h2>
        <ul className="text-sm">
          {datasets.map((d) => (
            <li key={d.id}>{d.name} — {d.examples.length} examples</li>
          ))}
        </ul>
      </section>

      <section aria-label="Runs" className="space-y-1">
        <h2 className="font-medium">Runs ({runs.length})</h2>
        <ul className="text-sm">
          {runs.map((r) => (
            <li key={r.run_id} className="flex items-center gap-2">
              <button type="button" onClick={() => setSelectedRun(r)} className="underline">{r.run_id}</button>
              <span>{String((r.config_snapshot?.retrieval as { mode?: string } | undefined)?.mode ?? "—")}</span>
              <button type="button" onClick={() => handleReplay(r.run_id)} className="text-xs text-neutral-500 hover:underline" disabled={busy}>replay</button>
            </li>
          ))}
        </ul>
      </section>

      {selectedRun && (
        <section aria-label="Run detail" className="space-y-2 rounded-lg border p-4">
          <h2 className="font-medium">Run {selectedRun.run_id}</h2>
          <p className="text-xs text-neutral-500">replay_of: {String(selectedRun.config_snapshot?.replay_of ?? "—")}</p>
          <pre className="overflow-auto rounded bg-neutral-100 p-2 text-xs dark:bg-neutral-900">
            {JSON.stringify(selectedRun.results ?? {}, null, 2)}
          </pre>
          <pre className="overflow-auto rounded bg-neutral-100 p-2 text-xs dark:bg-neutral-900">
            {JSON.stringify({ cost: selectedRun.cost, latency_ms: selectedRun.latency_ms, tokens: selectedRun.tokens, accounting_complete: selectedRun.accounting_complete }, null, 2)}
          </pre>
          <label className="flex items-center gap-2 text-xs">
            <input type="checkbox" checked={showSourceText} onChange={(e) => setShowSourceText(e.target.checked)} />
            show source text (opt-in)
          </label>
          <p className="text-xs text-neutral-500">Source text hidden by default (privacy).</p>
        </section>
      )}

      <section aria-label="Compare runs" className="space-y-2 rounded-lg border p-4">
        <h2 className="font-medium">A/B comparison</h2>
        <input className="w-full rounded border px-2 py-1" aria-label="Baseline run id" placeholder="baseline run id" disabled={busy} value={cmpBaseline} onChange={(e) => setCmpBaseline(e.target.value)} />
        <input className="w-full rounded border px-2 py-1" aria-label="Candidate run id" placeholder="candidate run id" disabled={busy} value={cmpCandidate} onChange={(e) => setCmpCandidate(e.target.value)} />
        <input className="w-full rounded border px-2 py-1" aria-label="Comparison dimension" placeholder="dimension (mode/top_k/reranker)" disabled={busy} value={cmpDimension} onChange={(e) => setCmpDimension(e.target.value)} />
        <button type="button" onClick={handleCompare} disabled={busy} aria-disabled={busy} className="rounded bg-blue-600 px-3 py-1 text-white disabled:opacity-50">Compare</button>
        {cmpResult && <pre className="overflow-auto rounded bg-neutral-100 p-2 text-xs dark:bg-neutral-900">{cmpResult}</pre>}
      </section>
    </main>
  );
}
