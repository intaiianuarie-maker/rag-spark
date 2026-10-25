import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FeedbackPayload } from "./stream";
import { streamQuestion } from "./stream";

const backendFetch = vi.fn<(input: RequestInfo, init?: RequestInit) => Promise<Response>>();

function sseFrame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function makeSseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(encoder.encode(f));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

beforeEach(() => {
  vi.stubGlobal("fetch", backendFetch as unknown as typeof fetch);
  backendFetch.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("streamQuestion (behavioral)", () => {
  it("parses token events into partial text incrementally in order", async () => {
    const frames = [
      sseFrame("token", { text: "Company " }),
      sseFrame("token", { text: "revenue " }),
      sseFrame("token", { text: "grew." }),
    ];
    backendFetch.mockResolvedValueOnce(makeSseResponse(frames));

    const out: string[] = [];
    for await (const ev of streamQuestion("ws-1", "q")) {
      if (ev.type === "token") out.push(ev.text);
    }
    expect(out).toEqual(["Company ", "revenue ", "grew."]);
    expect(out.join("")).toBe("Company revenue grew.");
  });

  it("emits the citations event once, only at the end, with full payload", async () => {
    const answer = {
      answer: "Company revenue grew.",
      status: "answered",
      reason_code: null,
      reason: null,
      citations: [{ chunk_id: "chunk-1" }],
      claims: [{ text: "Company revenue grew.", chunk_ids: ["chunk-1"] }],
      confidence: { level: "high", basis: "x" },
      trace_id: "t1",
      publication_status: "verified",
    };
    const frames = [
      sseFrame("token", { text: "Company " }),
      sseFrame("token", { text: "revenue grew." }),
      sseFrame("citations", answer),
    ];
    backendFetch.mockResolvedValueOnce(makeSseResponse(frames));

    const events: { type: string }[] = [];
    let citationsCount = 0;
    let citationsAnswer: unknown = null;
    for await (const ev of streamQuestion("ws-1", "q")) {
      events.push({ type: ev.type });
      if (ev.type === "citations") {
        citationsCount += 1;
        citationsAnswer = ev.answer;
      }
    }
    expect(citationsCount).toBe(1);
    // citations must be the last event
    expect(events[events.length - 1].type).toBe("citations");
    expect(citationsAnswer).toMatchObject({ answer: "Company revenue grew.", trace_id: "t1" });
  });

  it("maps the error event code and message", async () => {
    const frames = [
      sseFrame("error", { error: "boom", code: "LLM_UNAVAILABLE", trace_id: "t2" }),
    ];
    backendFetch.mockResolvedValueOnce(makeSseResponse(frames));

    const events: { type: string; code?: string; message?: string; traceId?: string | null }[] = [];
    for await (const ev of streamQuestion("ws-1", "q")) {
      events.push(ev as { type: string; code?: string; message?: string; traceId?: string | null });
    }
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("error");
    expect(events[0].code).toBe("LLM_UNAVAILABLE");
    expect(events[0].message).toBe("boom");
    expect(events[0].traceId).toBe("t2");
  });

  it("terminates the iterator on abort with no further events", async () => {
    const controller = new AbortController();
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(encoder.encode(sseFrame("token", { text: "partial" })));
        const close = () => {
          try {
            c.close();
          } catch {
            // already closed
          }
        };
        if (controller.signal.aborted) close();
        else controller.signal.addEventListener("abort", close, { once: true });
      },
    });
    backendFetch.mockResolvedValueOnce(
      new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } }),
    );

    const collected: string[] = [];
    const run = (async () => {
      try {
        for await (const ev of streamQuestion("ws-1", "q", undefined, controller.signal)) {
          if (ev.type === "token") collected.push(ev.text);
        }
      } catch {
        // abort terminates iteration; ignore
      }
    })();
    setTimeout(() => controller.abort(), 50);
    await run;
    expect(collected).toEqual(["partial"]);
  });

  it("pins the FeedbackPayload shape", () => {
    const payload: FeedbackPayload = {
      turnId: "turn-1",
      rating: "positive",
      comment: "great",
      answeredAt: "2026-10-24T00:00:00.000Z",
    };
    expect(payload.turnId).toBe("turn-1");
    expect(payload.rating).toBe("positive");
    expect(payload.comment).toBe("great");
    expect(payload.answeredAt).toBe("2026-10-24T00:00:00.000Z");

    const minimal: FeedbackPayload = {
      turnId: "turn-2",
      rating: "none",
      answeredAt: "2026-10-24T00:00:00.000Z",
    };
    expect(minimal.comment).toBeUndefined();
  });
});

describe("M5-001a REV-2 Finding 4 / REV-3 residual — non-2xx SSE error frames", () => {
  it("surfaces the frame's code/message/trace_id on a 503", async () => {
    const frame =
      "event: error\ndata: " +
      JSON.stringify({
        error: "backend unavailable",
        code: "BACKEND_UNAVAILABLE",
        trace_id: "trace-9",
      }) +
      "\n\n";
    backendFetch.mockResolvedValueOnce(
      new Response(frame, {
        status: 503,
        headers: { "content-type": "text/event-stream" },
      }),
    );

    const events: { type: string; code?: string; message?: string; traceId?: string | null }[] = [];
    for await (const ev of streamQuestion("ws-1", "q")) {
      events.push(ev as { type: string; code?: string; message?: string; traceId?: string | null });
    }
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("error");
    expect(events[0].code).toBe("BACKEND_UNAVAILABLE");
    expect(events[0].message).toBe("backend unavailable");
    expect(events[0].traceId).toBe("trace-9");
  });

  it("surfaces the frame's code/message/trace_id on a 403", async () => {
    const frame =
      "event: error\ndata: " +
      JSON.stringify({
        error: "not allowed",
        code: "ACL_DENIED",
        trace_id: "trace-7",
      }) +
      "\n\n";
    backendFetch.mockResolvedValueOnce(
      new Response(frame, {
        status: 403,
        headers: { "content-type": "text/event-stream" },
      }),
    );

    const events: { type: string; code?: string; message?: string; traceId?: string | null }[] = [];
    for await (const ev of streamQuestion("ws-1", "q")) {
      events.push(ev as { type: string; code?: string; message?: string; traceId?: string | null });
    }
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("error");
    expect(events[0].code).toBe("ACL_DENIED");
    expect(events[0].message).toBe("not allowed");
    expect(events[0].traceId).toBe("trace-7");
  });

  // REV-3 residual (POLICE): the page's turn-scope guard drops any event
  // without the caller's turnId. An error frame that arrives without it is
  // invisible to the page — the placeholder assistant stays blank. Every
  // yielded event must therefore carry the turnId, so the guard ACCEPTS it.
  it("carries the caller turnId on the SSE error event (the page guard accepts it)", async () => {
    const frame =
      "event: error\ndata: " +
      JSON.stringify({
        error: "not allowed",
        code: "ACL_DENIED",
        trace_id: "trace-7",
      }) +
      "\n\n";
    backendFetch.mockResolvedValueOnce(
      new Response(frame, {
        status: 403,
        headers: { "content-type": "text/event-stream" },
      }),
    );

    const events: { type: string; turnId?: string }[] = [];
    for await (const ev of streamQuestion("ws-1", "q", undefined, undefined, "turn-1")) {
      events.push(ev as { type: string; turnId?: string });
    }
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("error");
    expect(events[0].turnId).toBe("turn-1");
  });

  it("carries the caller turnId on the JSON-envelope fallback error", async () => {
    backendFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: { code: "HTTP_500", message: "boom" } }), {
        status: 500,
        headers: { "content-type": "application/json" },
      }),
    );

    const events: { type: string; code?: string; turnId?: string }[] = [];
    for await (const ev of streamQuestion("ws-1", "q", undefined, undefined, "turn-2")) {
      events.push(ev as { type: string; code?: string; turnId?: string });
    }
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("error");
    expect(events[0].code).toBe("HTTP_500");
    expect(events[0].turnId).toBe("turn-2");
  });

  it("carries the caller turnId on the no-body error", async () => {
    backendFetch.mockResolvedValueOnce(
      new Response(null, { status: 200, headers: { "content-type": "text/event-stream" } }),
    );

    const events: { type: string; code?: string; turnId?: string }[] = [];
    for await (const ev of streamQuestion("ws-1", "q", undefined, undefined, "turn-3")) {
      events.push(ev as { type: string; code?: string; turnId?: string });
    }
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("error");
    expect(events[0].code).toBe("no_body");
    expect(events[0].turnId).toBe("turn-3");
  });
});

describe("M5-001a REV-2 Finding 3 — read failures and intentional abort", () => {
  it("surfaces a reader rejection as an error event (no unhandled rejection)", async () => {
    const controller = new AbortController();
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(encoder.encode(sseFrame("token", { text: "partial" })));
        // Reject the reader mid-pump: the generator must surface this as an
        // error event, not throw an unhandled rejection out of the pump.
        setTimeout(() => {
          c.error(new Error("read boom"));
        }, 5);
      },
    });
    backendFetch.mockResolvedValueOnce(
      new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } }),
    );

    const events: { type: string; code?: string; message?: string }[] = [];
    let threw = false;
    try {
      for await (const ev of streamQuestion("ws-1", "q", undefined, controller.signal)) {
        events.push(ev as { type: string; code?: string; message?: string });
      }
    } catch {
      threw = true;
    }
    // The generator never throws: the read failure is delivered as an event.
    expect(threw).toBe(false);
    expect(events.some((e) => e.type === "token")).toBe(true);
    expect(events.some((e) => e.type === "error" && e.code === "stream_read_error")).toBe(true);
  });

  it("intentional AbortController.abort() terminates cleanly with no error event", async () => {
    const controller = new AbortController();
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(encoder.encode(sseFrame("token", { text: "partial" })));
        // Never close — the abort must be the thing that ends the stream.
        controller.signal.addEventListener("abort", () => {
          c.error(new DOMException("The operation was aborted", "AbortError"));
        });
      },
    });
    backendFetch.mockResolvedValueOnce(
      new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } }),
    );

    const events: { type: string }[] = [];
    const run = (async () => {
      for await (const ev of streamQuestion("ws-1", "q", undefined, controller.signal)) {
        events.push(ev);
      }
    })();
    setTimeout(() => controller.abort(), 20);
    await run;
    // No error event: intentional abort is a clean stop.
    expect(events.every((e) => e.type !== "error")).toBe(true);
    expect(events.some((e) => e.type === "token")).toBe(true);
  });
});
