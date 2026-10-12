/**
 * T-V1-013 — hostile multipart upload fixture (RP-07 / QF-07, depends T-V1-012).
 *
 * A byte-EXACT multipart/form-data body containing invalid-UTF-8 file bytes,
 * used to prove that the /api/backend proxy moves upload bytes verbatim:
 * length + SHA-256 must be identical pre-transport → at the upload handler →
 * stored, and every multipart field must survive the trip intact.
 *
 * The body is *constructed* from explicit byte arrays rather than stored as a
 * text file, so no editor / git filter / encoding round-trip can ever "repair"
 * the hostile bytes — what this module exports is exactly what goes on the
 * wire.
 *
 * Hostility inventory (all of it inside the file part payload):
 *   - NUL bytes and invalid lead bytes 0xFF / 0xFE;
 *   - lone continuation bytes (0x80 … 0xBF);
 *   - overlong encodings of "/" (C0 AF, E0 80 AF) and of U+007F (C1 BF);
 *   - an UTF-16 surrogate half (ED A0 80) — CESU-8, invalid UTF-8;
 *   - code-point leads beyond U+10FFFF (F5 …, F8 …);
 *   - a truncated multi-byte sequence as the payload's FINAL bytes;
 *   - boundary LOOKALIKES that are RFC-legal (the boundary value itself must
 *     never appear in the body) — a case-swapped `--KIOS-…` line and a
 *     truncated `--kios-tv1013-bound` prefix — so a naive case-insensitive or
 *     prefix-based split mis-parses while a correct delimiter scan does not;
 *   - 512 deterministic pseudo-random bytes (seeded LCG) so the body spans
 *     several stream chunks with no human-readable structure.
 *
 * The text fields stay valid UTF-8 (that is what a browser actually sends):
 * field intactness there is proven by the whole-body SHA plus a multipart
 * re-parse; file-part intactness is proven by SHA of the sliced-out bytes.
 *
 * No runtime dependency: Node/`node:crypto` only (test-fixture code).
 */
import { createHash } from "node:crypto";

/** Multipart boundary value (as sent in the `boundary=` parameter). */
export const HOSTILE_BOUNDARY = "----kios-tv1013-boundary";
/** Verbatim `content-type` the proxy must forward, `boundary=…` included. */
export const HOSTILE_CONTENT_TYPE = `multipart/form-data; boundary=${HOSTILE_BOUNDARY}`;

/** Text field 1 — valid UTF-8, non-ASCII (catches decode/re-encode churn). */
export const TITLE_FIELD = "Ünïcødé “résumé” — µ-ticket 日本語 ✅";
/** Text field 2 — ASCII path-ish value. */
export const COLLECTION_FIELD = "evidence/2026-µ";
/** Text field 3 — plain ASCII. */
export const WORKSPACE_FIELD = "ws-1";
/** File part disposition values (non-ASCII filename on purpose). */
export const FILE_FILENAME = "hostile-µ-ticket.bin";
export const FILE_CONTENT_TYPE = "application/octet-stream";

function hostileFileBytes(): Buffer {
  const push = (...bytes: number[]): Buffer => Buffer.from(bytes);
  const chunks: Buffer[] = [
    // NULs + invalid lead bytes.
    push(0x00, 0xff, 0xfe, 0x00, 0xff, 0xfe, 0x00),
    // Lone continuation bytes (0x80…0xBF) with an embedded newline.
    push(0x80, 0x8f, 0x9f, 0xa0, 0xbf, 0x0a),
    // Overlong "/" in 2-byte (C0 AF) and 3-byte (E0 80 AF) form.
    push(0xc0, 0xaf, 0xe0, 0x80, 0xaf),
    // Overlong U+007F (C1 BF) + UTF-16 surrogate half U+D800 (ED A0 80).
    push(0xc1, 0xbf, 0xed, 0xa0, 0x80),
    // Beyond U+10FFFF: F5-lead and F8-lead sequences.
    push(0xf5, 0x80, 0x80, 0x80, 0xf8, 0x88, 0x80, 0x80, 0x80),
    // Valid UTF-8 mixed in — must survive byte-for-byte too.
    Buffer.from("café ✅ 日本語\rmixed\n", "utf8"),
    // Boundary LOOKALIKES (RFC-legal: neither contains HOSTILE_BOUNDARY).
    Buffer.from("decoy: --KIOS-TV1013-BOUNDARY-- and --kios-tv1013-bound\r\n", "utf8"),
    // 512 deterministic pseudo-random bytes (LCG, seed 0x5EED) so the payload
    // is bulk binary rather than a hand-picked 18-byte snippet.
    pseudoRandom(512),
    // Truncated multi-byte sequence as the payload's FINAL bytes.
    push(0xe2, 0x82),
  ];
  return Buffer.concat(chunks);
}

/** Deterministic byte soup — same bytes on every machine, every run. */
function pseudoRandom(length: number): Buffer {
  const out = Buffer.alloc(length);
  let state = 0x5eed;
  for (let i = 0; i < length; i += 1) {
    state = (state * 1103515245 + 12345) & 0x7fffffff;
    out[i] = (state >>> 16) & 0xff;
  }
  return out;
}

const ascii = (text: string): Buffer => Buffer.from(text, "utf8");

/** Everything up to (but excluding) the file part's raw bytes. */
export const HOSTILE_MULTIPART_HEAD: Buffer = Buffer.concat([
  ascii(`--${HOSTILE_BOUNDARY}\r\n`),
  ascii('Content-Disposition: form-data; name="title"\r\n\r\n'),
  ascii(`${TITLE_FIELD}\r\n`),
  ascii(`--${HOSTILE_BOUNDARY}\r\n`),
  ascii('Content-Disposition: form-data; name="collection"\r\n\r\n'),
  ascii(`${COLLECTION_FIELD}\r\n`),
  ascii(`--${HOSTILE_BOUNDARY}\r\n`),
  ascii('Content-Disposition: form-data; name="workspace"\r\n\r\n'),
  ascii(`${WORKSPACE_FIELD}\r\n`),
  ascii(`--${HOSTILE_BOUNDARY}\r\n`),
  ascii(`Content-Disposition: form-data; name="file"; filename="${FILE_FILENAME}"\r\n`),
  ascii(`Content-Type: ${FILE_CONTENT_TYPE}\r\n\r\n`),
]);

/** The closing boundary line. */
export const HOSTILE_MULTIPART_TAIL: Buffer = ascii(`\r\n--${HOSTILE_BOUNDARY}--\r\n`);

/** The file part's raw bytes — stage-1 reference for the file content. */
export const HOSTILE_FILE_BYTES: Buffer = hostileFileBytes();
/** The whole multipart body — exactly what crosses the proxy. */
export const HOSTILE_MULTIPART_BODY: Buffer = Buffer.concat([
  HOSTILE_MULTIPART_HEAD,
  HOSTILE_FILE_BYTES,
  HOSTILE_MULTIPART_TAIL,
]);
/** Byte offset where the file part's payload starts inside the body. */
export const FILE_PAYLOAD_OFFSET = HOSTILE_MULTIPART_HEAD.byteLength;

/**
 * Cut points, relative to `FILE_PAYLOAD_OFFSET`: inside the file header, at
 * the very first payload byte, mid-payload, and past the payload — so every
 * stream chunk boundary falls in a different kind of byte run.
 */
export const FIXTURE_CHUNK_BREAKS = [-40, 1, 11, 200, 530] as const;

/** Split the body into uneven chunks, several of them mid-header/mid-payload. */
export function fixtureChunks(body: Buffer = HOSTILE_MULTIPART_BODY): Uint8Array[] {
  const cuts = [
    0,
    ...FIXTURE_CHUNK_BREAKS.map((delta) => FILE_PAYLOAD_OFFSET + delta).filter(
      (at) => at > 0 && at < body.byteLength,
    ),
    body.byteLength,
  ].sort((a, b) => a - b);
  const chunks: Uint8Array[] = [];
  for (let i = 0; i < cuts.length - 1; i += 1) {
    if (cuts[i + 1] === cuts[i]) continue;
    chunks.push(body.subarray(cuts[i], cuts[i + 1]));
  }
  return chunks;
}

export function sha256(data: Uint8Array): string {
  return createHash("sha256").update(data).digest("hex");
}

/** len + SHA-256 fingerprint — the 3-stage equality assertion of T-V1-013. */
export function fingerprint(data: Uint8Array): { bytes: number; sha256: string } {
  return { bytes: data.byteLength, sha256: sha256(data) };
}
