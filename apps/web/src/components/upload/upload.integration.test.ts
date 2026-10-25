import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { isStaleFetch } from "./use-job-polling";
import { createJobPoller } from "./polling";

describe("stale cross-workspace/job response fence", () => {
  it("discards a response whose token is no longer latest", () => {
    expect(isStaleFetch(1, 2, "ws-a", "job-1", "ws-a", "job-1")).toBe(true);
  });

  it("discards a response when workspace changed mid-flight", () => {
    expect(isStaleFetch(1, 1, "ws-a", "job-1", "ws-b", "job-1")).toBe(true);
  });

  it("discards a response when job changed mid-flight", () => {
    expect(isStaleFetch(1, 1, "ws-a", "job-1", "ws-a", "job-2")).toBe(true);
  });

  it("accepts a response only when token and identity match", () => {
    expect(isStaleFetch(1, 1, "ws-a", "job-1", "ws-a", "job-1")).toBe(false);
  });
});

describe("failed-job retry re-queues (backend semantics)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("a fresh poller after a failed terminal fetches again", async () => {
    const fetchStatus = vi
      .fn()
      .mockResolvedValueOnce({
        stage: "failed",
        status: "failed",
        error_code: "PARSE_ERROR",
        error_message: "bad",
      } as never)
      .mockResolvedValue({ stage: "ready", status: "ready" } as never);

    const onError = vi.fn();
    const onTerminal = vi.fn();
    const first = createJobPoller(
      { fetchStatus, onStatus: vi.fn(), onError, onTerminal },
      { setTimeoutFn: setTimeout, clearTimeoutFn: clearTimeout },
    );
    first.start();
    await vi.advanceTimersByTimeAsync(2000);
    expect(onError).toHaveBeenCalled();
    expect(first.isTerminal()).toBe(true);

    // Retry re-queues: a new poller fetches again (simulating re-upload).
    const second = createJobPoller(
      { fetchStatus, onStatus: vi.fn(), onError: vi.fn(), onTerminal: vi.fn() },
      { setTimeoutFn: setTimeout, clearTimeoutFn: clearTimeout },
    );
    second.start();
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetchStatus).toHaveBeenCalledTimes(2);
  });
});

describe("cancel stops polling permanently", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("cancel prevents further fetches", async () => {
    const fetchStatus = vi.fn().mockResolvedValue({ stage: "queued", status: "queued" } as never);
    const poller = createJobPoller(
      { fetchStatus, onStatus: vi.fn(), onError: vi.fn(), onTerminal: vi.fn() },
      { setTimeoutFn: setTimeout, clearTimeoutFn: clearTimeout },
    );
    poller.start();
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
    poller.cancel();
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
  });
});
