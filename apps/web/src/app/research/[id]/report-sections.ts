/**
 * Research report render-contract helpers (T-V3-024).
 *
 * Pure, DOM-free decisions the reviewer checklist covers:
 *  1. exact clicks — every evidence row / contradiction side yields chunk deep
 *     links to the exact doc/version/chunk;
 *  2. disputed distinct — contradictions expose TWO sides, never a merged note;
 *  3. unknowns shown — the unknowns section is always built, never conditional
 *     on anything else being present.
 *
 * Kept separate from the page so the repo's existing test pattern (pure
 * builders, no DOM library — see ask/citations.test.ts) can pin them.
 */

/** Verdicts that count as disputed (mirror EvidenceRecord verdicts). */
const DISPUTED_VERDICTS = new Set(["DISPUTED", "CONTRADICTS", "OPPOSING_VERDICTS"]);

export type EvidenceRow = {
  statement: string;
  confidence?: string;
  limitations: string[];
  chunkLinks: string[];
  disputed: boolean;
};

export type ContradictionSide = {
  claim: string;
  chunkLinks: string[];
};

export type ContradictionRow = {
  question: string;
  reason: string;
  sideA: ContradictionSide;
  sideB: ContradictionSide;
};

export type UnknownRow = {
  reason: string;
  text: string;
};

export type ReportSections = {
  evidence: EvidenceRow[];
  contradictions: ContradictionRow[];
  unknowns: UnknownRow[];
  limitations: string[];
  abstained: boolean;
  abstentionReason: string;
};

export function isDisputed(item: Record<string, unknown>): boolean {
  const verdict = item.verdict;
  if (typeof verdict === "string" && DISPUTED_VERDICTS.has(verdict)) return true;
  return item.disputed === true;
}

export function chunkDeepLink(chunkId: string | undefined, documentId?: string | null): string | null {
  if (!chunkId) return null;
  return `/knowledge/documents/${documentId || "unknown"}/${chunkId}`;
}

function links(chunkIds: unknown, documentId?: unknown): string[] {
  if (!Array.isArray(chunkIds) || chunkIds.length === 0) return [];
  return chunkIds
    .map((cid) => chunkDeepLink(str(cid), str(documentId) || null))
    .filter((v): v is string => v !== null);
}

/** A loosely-typed record as it arrives on the wire (T-V3-022 sends dicts). */
type RawRecord = Record<string, unknown>;

function str(value: unknown): string {
  return value === undefined || value === null ? "" : String(value);
}

function strList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(str) : [];
}

export function buildReportSections(report: RawRecord): ReportSections {
  const sent = (report.grounded_sentences ?? []) as RawRecord[];
  const contra = (report.contradictions ?? []) as RawRecord[];
  const unk = (report.unknowns ?? []) as RawRecord[];

  const evidence: EvidenceRow[] = sent.map((item) => ({
    statement: str(item.statement),
    confidence: item.confidence ? str(item.confidence) : undefined,
    limitations: strList(item.limitations),
    chunkLinks: links(item.chunk_ids, item.document_id ?? item.chunk_id),
    disputed: isDisputed(item),
  }));

  const contradictions: ContradictionRow[] = contra.map((c) => ({
    question: str(c.question),
    reason: str(c.reason),
    sideA: {
      claim: str(c.claim_a),
      chunkLinks: links(c.chunk_ids_a),
    },
    sideB: {
      claim: str(c.claim_b),
      chunkLinks: links(c.chunk_ids_b),
    },
  }));

  const unknowns: UnknownRow[] = unk.map((u) => ({
    reason: str(u.reason),
    text: str(u.statement || u.question),
  }));

  return {
    evidence,
    contradictions,
    unknowns,
    limitations: strList(report.limitations),
    abstained: report.abstained === true,
    abstentionReason: str(report.abstention_reason),
  };
}
