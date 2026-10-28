export type JobStage =
  | "queued"
  | "parsing"
  | "chunking"
  | "embedding"
  | "indexing"
  | "ready"
  | "failed";

// Honest stage weights: parsing is cheap, embedding/indexing dominate.
const STAGE_WEIGHTS: Record<JobStage, number> = {
  queued: 0,
  parsing: 10,
  chunking: 25,
  embedding: 55,
  indexing: 80,
  ready: 100,
  failed: 0,
};

export function stageToProgress(stage: string | null | undefined): number {
  if (!stage) return 0;
  const key = stage.toLowerCase() as JobStage;
  if (key in STAGE_WEIGHTS) return STAGE_WEIGHTS[key];
  return 0;
}

export function isTerminalStage(stage: string | null | undefined): boolean {
  const s = (stage ?? "").toLowerCase();
  return s === "ready" || s === "failed";
}
