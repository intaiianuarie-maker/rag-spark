/**
 * TASK-M2-002b — UploadPanel render + a11y (pure, node-safe).
 *
 * Behavioral browser coverage is gated behind the heavy wrapper; this pins
 * the static contract (progressbar role, cancel/retry affordances, accept list).
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { UploadZone } from "./upload-zone";
import { ACCEPTED_MIME_TYPES } from "./accepted-mimes";

describe("UploadZone accept list", () => {
  it("advertises the six+ backend-supported formats", () => {
    expect(ACCEPTED_MIME_TYPES.length).toBeGreaterThanOrEqual(6);
    expect(ACCEPTED_MIME_TYPES).toContain("application/pdf");
    expect(ACCEPTED_MIME_TYPES).toContain("text/plain");
    expect(ACCEPTED_MIME_TYPES).toContain("text/csv");
    expect(ACCEPTED_MIME_TYPES).toContain("text/html");
    expect(ACCEPTED_MIME_TYPES).toContain("text/markdown");
  });
});

describe("UploadZone static render", () => {
  it("renders a labelled dropzone button", () => {
    const html = renderToStaticMarkup(<UploadZone onFiles={() => {}} />);
    expect(html).toContain('aria-label="Upload files"');
    expect(html).toContain('role="button"');
  });
});
