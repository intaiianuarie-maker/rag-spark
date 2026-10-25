import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createJobPoller, mapJobError } from "./polling";

describe("mapJobError", () => {
  it("maps every canonical reason code", async () => {
    expect(mapJobError("UNSUPPORTED_MIME_TYPE")).toBe("Unsupported file type.");
    expect(mapJobError("EMPTY_DOCUMENT")).toBe("Uploaded file is empty.");
    expect(mapJobError("DOCUMENT_TOO_LARGE")).toBe("File is too large.");
    expect(mapJobError("PARSE_ERROR")).toBe("Could not parse the document.");
    expect(mapJobError("QUEUE_UNAVAILABLE")).toBe("Ingestion queue is unavailable; try again shortly.");
  });

  it("falls back safely for unknown/empty/null codes", async () => {
    expect(mapJobError(null)).toBe("Ingestion failed.");
    expect(mapJobError(undefined)).toBe("Ingestion failed.");
    expect(mapJobError("")).toBe("Ingestion failed.");
    expect(mapJobError("SOMETHING_ELSE")).toBe("Ingestion failed.");
    expect(mapJobError("SOMETHING_ELSE", "   ")).toBe("Ingestion failed.");
    expect(mapJobError("SOMETHING_ELSE", "custom msg")).toBe("custom msg");
  });
});

describe("createJobPoller backoff + cancel + terminal", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("uses a 2000ms default interval", async () => {
    const fetchStatus = vi.fn().mockResolvedValue({ stage: "ready", status: "ready" } as never);
    const onTerminal = vi.fn();
    const poller = createJobPoller(
      { fetchStatus, onStatus: vi.fn(), onError: vi.fn(), onTerminal },
      { setTimeoutFn: setTimeout, clearTimeoutFn: clearTimeout },
    );
    poller.start();
    expect(fetchStatus).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
  });

  it("polls on start then every 2000ms; no double-scheduling", async () => {
    const fetchStatus = vi.fn().mockResolvedValue({ stage: "queued", status: "queued" } as never);
    const poller = createJobPoller(
      { fetchStatus, onStatus: vi.fn(), onError: vi.fn(), onTerminal: vi.fn() },
      { setTimeoutFn: setTimeout, clearTimeoutFn: clearTimeout },
    );
    poller.start();
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetchStatus).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetchStatus).toHaveBeenCalledTimes(3);
  });

  it("cancel stops polling permanently", async () => {
    const fetchStatus = vi.fn().mockResolvedValue({ stage: "queued", status: "queued" } as never);
    const poller = createJobPoller(
      { fetchStatus, onStatus: vi.fn(), onError: vi.fn(), onTerminal: vi.fn() },
      { setTimeoutFn: setTimeout, clearTimeoutFn: clearTimeout },
    );
    poller.start();
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
    poller.cancel();
    expect(poller.isCancelled()).toBe(true);
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
  });

  it("terminal (ready) triggers onTerminal once and stops the loop", async () => {
    const fetchStatus = vi.fn().mockResolvedValue({ stage: "ready", status: "ready" } as never);
    const onTerminal = vi.fn();
    const poller = createJobPoller(
      { fetchStatus, onStatus: vi.fn(), onError: vi.fn(), onTerminal },
      { setTimeoutFn: setTimeout, clearTimeoutFn: clearTimeout },
    );
    poller.start();
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
    expect(onTerminal).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
    expect(poller.isTerminal()).toBe(true);
  });

  it("terminal (failed) triggers onError + onTerminal once and stops", async () => {
    const fetchStatus = vi.fn().mockResolvedValue({
      stage: "failed",
      status: "failed",
      error_code: "PARSE_ERROR",
      error_message: "bad",
    } as never);
    const onError = vi.fn();
    const onTerminal = vi.fn();
    const poller = createJobPoller(
      { fetchStatus, onStatus: vi.fn(), onError, onTerminal },
      { setTimeoutFn: setTimeout, clearTimeoutFn: clearTimeout },
    );
    poller.start();
    await vi.advanceTimersByTimeAsync(2000);
    expect(onError).toHaveBeenCalledWith("Could not parse the document.");
    expect(onTerminal).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
  });

  it("injected interval override is respected", async () => {
    const fetchStatus = vi.fn().mockResolvedValue({ stage: "ready", status: "ready" } as never);
    const poller = createJobPoller(
      { fetchStatus, onStatus: vi.fn(), onError: vi.fn(), onTerminal: vi.fn() },
      { intervalMs: 500, setTimeoutFn: setTimeout, clearTimeoutFn: clearTimeout },
    );
    poller.start();
    await vi.advanceTimersByTimeAsync(500);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
  });
});

describe("createJobPoller pause/resume (hidden tab)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not fetch while paused", async () => {
    const fetchStatus = vi.fn().mockResolvedValue({ stage: "queued", status: "queued" } as never);
    const poller = createJobPoller(
      { fetchStatus, onStatus: vi.fn(), onError: vi.fn(), onTerminal: vi.fn() },
      { setTimeoutFn: setTimeout, clearTimeoutFn: clearTimeout },
    );
    poller.start();
    poller.pause();
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchStatus).not.toHaveBeenCalled();
  });

  it("resume restarts the schedule and a fetch fires", async () => {
    const fetchStatus = vi.fn().mockResolvedValue({ stage: "queued", status: "queued" } as never);
    const poller = createJobPoller(
      { fetchStatus, onStatus: vi.fn(), onError: vi.fn(), onTerminal: vi.fn() },
      { setTimeoutFn: setTimeout, clearTimeoutFn: clearTimeout },
    );
    poller.start();
    poller.pause();
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchStatus).not.toHaveBeenCalled();
    poller.resume();
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
  });

  it("hidden visibility prevents fetch during tick", async () => {
    let visible = false;
    const fetchStatus = vi.fn().mockResolvedValue({ stage: "queued", status: "queued" } as never);
    const poller = createJobPoller(
      {
        fetchStatus,
        onStatus: vi.fn(),
        onError: vi.fn(),
        onTerminal: vi.fn(),
        onVisibilityChange: () => (visible ? "visible" : "hidden"),
      },
      { setTimeoutFn: setTimeout, clearTimeoutFn: clearTimeout },
    );
    poller.start();
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetchStatus).not.toHaveBeenCalled();
    visible = true;
    poller.resume();
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetchStatus).toHaveBeenCalled();
  });
});
