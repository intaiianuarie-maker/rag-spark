import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

// Minimal vitest config: resolve the app's `@/*` alias (tsconfig paths) so
// route modules importable by tests (e.g. the /api/backend proxy regression
// tests for the M1-002b REWORK). Default environment (node); no plugins.
export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "node",
  },
});
