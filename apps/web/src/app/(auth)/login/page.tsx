import { redirect } from "next/navigation";
import type { Metadata } from "next";
import { Suspense } from "react";
import { cookies } from "next/headers";
import { ACCESS_COOKIE } from "../api/auth/_lib/cookies";
import { sanitizeNextPath } from "@/lib/auth-client.server";
import { LoginForm } from "./login-form";

export const metadata: Metadata = {
  title: "Sign in",
};

/**
 * Blocking route (cacheComponents: `instant = false`): the cookie check must
 * happen BEFORE the shell renders — an authenticated visitor must never even
 * momentarily see the sign-in form.
 */
export const instant = false;

/** Skeleton shown while the authed-redirect check resolves (prerender only;
 * at request time this resolves immediately server-side). */
function LoginFallback() {
  return (
    <form
      aria-busy="true"
      className="space-y-4"
      onSubmit={undefined}
    >
      <div className="h-10 animate-pulse rounded-lg bg-neutral-200 dark:bg-neutral-800" />
      <div className="h-10 animate-pulse rounded-lg bg-neutral-200 dark:bg-neutral-800" />
      <div className="h-9 animate-pulse rounded-lg bg-neutral-200 dark:bg-neutral-800" />
      <p className="sr-only">Loading sign-in form…</p>
    </form>
  );
}

/** Authenticated visitors go to their target (or /) per the boss ruling; the
 * `?next=` target is validated to be a same-origin, non-auth path. */
async function LoginContent({
  searchParams,
}: {
  searchParams: Promise<{ next?: string }>;
}) {
  const [{ next }, jar] = await Promise.all([searchParams, cookies()]);

  if (jar.get(ACCESS_COOKIE)?.value) {
    redirect(sanitizeNextPath(next) ?? "/");
  }

  return <LoginForm nextTarget={sanitizeNextPath(next) ?? null} />;
}

export default function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string }>;
}) {
  return (
    <Suspense fallback={<LoginFallback />}>
      <LoginContent searchParams={searchParams} />
    </Suspense>
  );
}
