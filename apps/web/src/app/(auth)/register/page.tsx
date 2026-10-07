import { redirect } from "next/navigation";
import type { Metadata } from "next";
import { Suspense } from "react";
import { cookies } from "next/headers";
import { ACCESS_COOKIE } from "../api/auth/_lib/cookies";
import { sanitizeNextPath } from "@/lib/auth-client.server";
import { RegisterForm } from "./register-form";

export const metadata: Metadata = {
  title: "Register",
};

/**
 * Blocking route (cacheComponents: `instant = false`): the cookie check must
 * happen BEFORE the shell renders — an authenticated visitor must never even
 * momentarily see the registration form.
 */
export const instant = false;

/** Skeleton while the authed-redirect check resolves (prerender snapshot only). */
function RegisterFallback() {
  return (
    <div aria-busy="true" className="space-y-4">
      <div className="h-10 animate-pulse rounded-lg bg-neutral-200 dark:bg-neutral-800" />
      <div className="h-10 animate-pulse rounded-lg bg-neutral-200 dark:bg-neutral-800" />
      <div className="h-10 animate-pulse rounded-lg bg-neutral-200 dark:bg-neutral-800" />
      <div className="h-9 animate-pulse rounded-lg bg-neutral-200 dark:bg-neutral-800" />
      <p className="sr-only">Loading registration form…</p>
    </div>
  );
}

/** Same authed-redirect + ?next validation rules as /login. */
async function RegisterContent({
  searchParams,
}: {
  searchParams: Promise<{ next?: string }>;
}) {
  const [{ next }, jar] = await Promise.all([searchParams, cookies()]);

  if (jar.get(ACCESS_COOKIE)?.value) {
    redirect(sanitizeNextPath(next) ?? "/");
  }

  return <RegisterForm nextTarget={sanitizeNextPath(next) ?? null} />;
}

export default function RegisterPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string }>;
}) {
  return (
    <Suspense fallback={<RegisterFallback />}>
      <RegisterContent searchParams={searchParams} />
    </Suspense>
  );
}
