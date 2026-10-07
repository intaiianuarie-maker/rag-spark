import { NextResponse } from "next/server";
import {
  extractBackendError,
  postToAuthBackend,
} from "../_lib/backend";

/**
 * Same-origin register proxy. 201 from upstream → 200 {ok:true}; 409/422 →
 * upstream status with the normalized error (fields for inline UI mapping).
 * Registering does NOT log the user in; the register form links to /login.
 */
export async function POST(request: Request): Promise<Response> {
  let payload: unknown = null;
  try {
    payload = await request.json();
  } catch {
    payload = null;
  }

  const outcome = await postToAuthBackend("/auth/register", payload);
  if (outcome.kind === "unreachable") {
    return NextResponse.json(
      {
        ok: false,
        error: {
          code: "backend_unreachable",
          message: "Authentication temporarily unavailable. Please retry shortly.",
        },
      },
      { status: 502 },
    );
  }

  if (outcome.status === 201) {
    return NextResponse.json({ ok: true });
  }

  const extracted = extractBackendError(outcome.body);
  return NextResponse.json(
    {
      ok: false,
      error: { code: extracted.code, message: extracted.message },
      ...(extracted.fields !== undefined ? { fields: extracted.fields } : {}),
    },
    { status: outcome.status },
  );
}
