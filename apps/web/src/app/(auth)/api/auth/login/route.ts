import { NextResponse } from "next/server";
import {
  extractBackendError,
  postToAuthBackend,
} from "../_lib/backend";
import { parseTokenPair, setAuthCookies } from "../_lib/cookies";

/**
 * Same-origin login proxy. Binding contract (M1-001a): upstream returns the
 * TokenPair in the JSON body; THIS route owns all Set-Cookie (httpOnly).
 * Success → 200 {ok:true}. Failure → <upstream status> {ok:false,error:{code,message}}.
 */
export async function POST(request: Request): Promise<Response> {
  let payload: unknown = null;
  try {
    payload = await request.json();
  } catch {
    payload = null;
  }

  const outcome = await postToAuthBackend("/auth/login", payload);
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

  if (outcome.status === 200) {
    const tokens = parseTokenPair(outcome.body);
    if (tokens === null) {
      return NextResponse.json(
        {
          ok: false,
          error: {
            code: "unexpected_response",
            message: "Login service responded unexpectedly. Please retry.",
          },
        },
        { status: 502 },
      );
    }
    const response = NextResponse.json({ ok: true });
    setAuthCookies(response, tokens);
    return response;
  }

  const extracted = extractBackendError(outcome.body);
  return NextResponse.json(
    {
      ok: false,
      error: { code: extracted.code, message: extracted.message },
      ...(outcome.retryAfter !== undefined ? { retryAfter: outcome.retryAfter } : {}),
    },
    { status: outcome.status },
  );
}
