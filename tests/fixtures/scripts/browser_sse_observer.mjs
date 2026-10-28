// Passive browser-side observation for the real no-store Ask fetch. Playwright
// CDP response.text() can lose these bodies after EOF. This tees the ACTUAL
// response, never replaces it, and sends no additional network requests.
const STORE = "__releaseAskSSEObserver";

// Self-contained because Playwright serializes this function into each NEW
// document. Exported also for small native-Response tests without a browser.
export function initializeAskStreamObserver({ path, maxBytes, captureTimeoutMs }) {
  const originalFetch = window.fetch;
  const store = { path, armed: null };
  Object.defineProperty(window, "__releaseAskSSEObserver", { value: store });

  async function capture(response, entry) {
    let reader, timer;
    const fail = code => {
      if (entry.state !== "pending") return;
      entry.state = "failed";
      entry.code = code;
      // Cancelling the clone must never abort the application's fetch. A tee
      // cancellation promise can wait for its other branch: do NOT await it.
      if (reader) void reader.cancel().catch(() => {});
    };
    try {
      reader = response.clone().body?.getReader();
      if (!reader) { fail("observer_body_missing"); return; }
      timer = setTimeout(() => fail("observer_capture_timeout"), captureTimeoutMs);
      const decoder = new TextDecoder();
      const chunks = [];
      let bytes = 0;
      while (entry.state === "pending") {
        const chunk = await reader.read();
        if (entry.state !== "pending") return;
        if (chunk.done) {
          chunks.push(decoder.decode());
          entry.wire = chunks.join("");
          entry.bytes = bytes;
          entry.state = "completed";
          return;
        }
        bytes += chunk.value.byteLength;
        if (bytes > maxBytes) { fail("observer_body_too_large"); return; }
        chunks.push(decoder.decode(chunk.value, { stream: true }));
      }
    } catch {
      fail("observer_clone_read_failed");
    } finally {
      clearTimeout(timer);
      try { reader?.releaseLock(); } catch { /* No effect on original response. */ }
    }
  }

  window.fetch = function (...args) {
    let entry;
    try {
      const [input, init] = args;
      const requestURL = typeof input === "string" || input instanceof URL ? input : input?.url;
      const url = new URL(requestURL, window.location.href);
      const method = String(init?.method ?? input?.method ?? "GET").toUpperCase();
      // Do not read ANY other route's request body (login/password included).
      if (url.origin === window.location.origin && url.pathname === path && method === "POST" && store.armed) {
        const query = typeof init?.body === "string" ? JSON.parse(init.body)?.query : undefined;
        if (query === store.armed.query) {
          entry = { query, path, state: "pending", code: null, wire: null, http_status: null };
          store.armed.entries.push(entry);
        }
      }
    } catch { /* Observation cannot alter fetch input parsing/behavior. */ }

    let pending;
    try { pending = Reflect.apply(originalFetch, this, args); }
    catch (error) {
      if (entry) { entry.state = "failed"; entry.code = "observer_fetch_failed"; }
      throw error;
    }
    if (entry) {
      // Attach both rejection paths immediately and return the ORIGINAL
      // promise. Clone synchronously in the fulfillment handler, before the
      // application's await continuation can lock the original body.
      void pending.then(response => {
        entry.http_status = response.status;
        void capture(response, entry).catch(() => {
          entry.state = "failed"; entry.code = "observer_clone_read_failed";
        });
      }, () => { entry.state = "failed"; entry.code = "observer_fetch_failed"; }).catch(() => {
        entry.state = "failed"; entry.code = "observer_clone_read_failed";
      });
    }
    return pending;
  };
}

export async function installAskStreamObserver(page, workspaceId, timeout) {
  await page.addInitScript(initializeAskStreamObserver, {
    path: `/api/backend/v1/workspaces/${workspaceId}/ask/stream`,
    maxBytes: 2 * 1024 * 1024,
    captureTimeoutMs: timeout,
  });
}

export async function armAskStreamObserver(page, query) {
  return page.evaluate(({ key, expected }) => {
    const store = window[key];
    if (!store) return false;
    store.armed = { query: expected, entries: [] };
    return true;
  }, { key: STORE, expected: query });
}

export async function readObservedAskStream(page, query, timeout) {
  try {
    await page.waitForFunction(({ key, expected }) => {
      const armed = window[key]?.armed;
      return armed?.query === expected && (armed.entries.length > 1 ||
        (armed.entries.length === 1 && armed.entries[0].state !== "pending"));
    }, { key: STORE, expected: query }, { timeout, polling: 50 });
    return await page.evaluate(({ key, expected }) => {
      const armed = window[key]?.armed;
      if (!armed || armed.query !== expected) return { ok: false, code: "observer_not_armed" };
      if (armed.entries.length !== 1) return { ok: false, code: "observer_duplicate_requests" };
      const entry = armed.entries[0];
      if (entry.state !== "completed") return { ok: false, code: entry.code || "observer_incomplete" };
      return { ok: true, wire: entry.wire, http_status: entry.http_status,
        bytes: entry.bytes, request_count: armed.entries.length };
    }, { key: STORE, expected: query });
  } catch {
    // Never expose Playwright call logs or captured source/answer text.
    return { ok: false, code: "observer_wait_failed" };
  }
}
