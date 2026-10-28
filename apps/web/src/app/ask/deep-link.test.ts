/**
 * TASK-M4-002b — source panel URL-hash deep links.
 *
 * serializeSourceRef / parseSourceRef: stable round-trip, tolerant of missing
 * / garbage / cross-workspace hashes (panel stays closed, never crashes).
 * Only workspace-scoped content is ever serialized.
 */
import { describe, expect, it } from "vitest";

import { parseSourceRef, serializeSourceRef, type SourceRef } from "./components/source-panel";

const WORKSPACE = "ws-1";
const OTHER_WORKSPACE = "ws-2";

function ref(overrides: Partial<SourceRef> = {}): SourceRef {
  return { workspaceId: WORKSPACE, messageIndex: 2, chunkId: "chunk-a", ...overrides };
}

describe("serializeSourceRef / parseSourceRef round-trip", () => {
  it("parses back exactly what was serialized", () => {
    const original = ref();
    expect(parseSourceRef(serializeSourceRef(original), WORKSPACE)).toEqual(original);
  });

  it("round-trips index 0 and a long chunk id", () => {
    const original = ref({ messageIndex: 0, chunkId: "a-very-long-chunk-id-12345" });
    expect(parseSourceRef(serializeSourceRef(original), WORKSPACE)).toEqual(original);
  });

  it("produces a hash fragment (leading #) that can be assigned to location.hash", () => {
    expect(serializeSourceRef(ref())).toMatch(/^#source=/);
  });
});

describe("parseSourceRef tolerates bad input", () => {
  it.each(["", "#", "#source=", "#source=", "source=abc", "random"])(
    "missing/empty hash returns null: %s",
    (hash) => {
      expect(parseSourceRef(hash, WORKSPACE)).toBeNull();
    },
  );

  it.each([
    "#source=not-json",
    "#source=%7B%22bad%22", // truncated JSON
    "#source=%5B1%2C2%5D", // array, not a ref object
    "#source=%7B%7D", // empty object
    "#source=%7B%22messageIndex%22%3A0%2C%22chunkId%22%3A%22c%22%7D", // missing workspaceId
    "#source=%7B%22workspaceId%22%3A%22ws%22%2C%22chunkId%22%3A%22c%22%7D", // missing messageIndex
    "#source=%7B%22workspaceId%22%3A%22ws%22%2C%22messageIndex%22%3A0%7D", // missing chunkId
  ])("malformed payload returns null: %s", (hash) => {
    expect(parseSourceRef(hash, WORKSPACE)).toBeNull();
  });

  it.each([
    { messageIndex: -1 },
    { messageIndex: 1.5 },
    { messageIndex: "2" },
    { chunkId: "" },
    { chunkId: 123 as unknown as string },
    { workspaceId: "" },
  ])("invalid field types return null: %j", (overrides) => {
    expect(parseSourceRef(serializeSourceRef(ref(overrides as Partial<SourceRef>)), WORKSPACE)).toBeNull();
  });
});

describe("parseSourceRef workspace scoping", () => {
  it("refuses a hash captured in another workspace (no cross-workspace leakage)", () => {
    const foreign = ref({ workspaceId: OTHER_WORKSPACE });
    expect(parseSourceRef(serializeSourceRef(foreign), WORKSPACE)).toBeNull();
  });

  it("returns null when there is no active workspace", () => {
    expect(parseSourceRef(serializeSourceRef(ref()), null)).toBeNull();
    expect(parseSourceRef(serializeSourceRef(ref()), undefined as unknown as string | null)).toBeNull();
  });

  it("accepts the ref only for the workspace it was captured in", () => {
    const original = ref();
    expect(parseSourceRef(serializeSourceRef(original), WORKSPACE)).toEqual(original);
    expect(parseSourceRef(serializeSourceRef(original), OTHER_WORKSPACE)).toBeNull();
  });
});
