"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { auth, type AuthFailure } from "@/lib/auth-client";

/** Sign-in form (M1-002a). Loading (submitting), error (banner + inline
 * field errors), and rate-limit retry hint states; no console noise. */
export function LoginForm({ nextTarget }: { nextTarget: string | null }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [failure, setFailure] = useState<AuthFailure | null>(null);

  const fieldErrors = failure?.fields;
  const emailError = fieldErrors?.email?.[0];
  const passwordError = fieldErrors?.password?.[0];

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    setFailure(null);
    setSubmitting(true);
    try {
      const result = await auth.login(email, password);
      if (result.ok) {
        // Resume after login: land on the validated ?next target, or home.
        router.replace(nextTarget ?? "/");
        router.refresh(); // re-render server components with new cookies
        return;
      }
      setFailure(result.failure);
    } catch {
      setFailure({
        code: "network_error",
        message: "Something went wrong. Please try again.",
        status: 0,
      });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4" noValidate>
      {failure !== null && (
        <div
          role="alert"
          className="rounded-lg border border-red-600/30 bg-red-50 p-3 text-sm text-red-900 dark:border-red-400/30 dark:bg-red-950/40 dark:text-red-200"
        >
          <p>{failure.message}</p>
          {failure.code === "rate_limited" && failure.retryAfter !== undefined && (
            <p className="mt-1 text-xs opacity-80">
              Too many attempts — retry after about {failure.retryAfter}s.
            </p>
          )}
        </div>
      )}

      <div className="space-y-1.5">
        <Label htmlFor="email">Email</Label>
        <Input
          id="email"
          name="email"
          type="email"
          autoComplete="email"
          placeholder="you@example.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          aria-invalid={emailError ? true : undefined}
          aria-describedby={emailError ? "email-error" : undefined}
          disabled={submitting}
          required
        />
        {emailError && (
          <p id="email-error" className="text-xs text-red-700 dark:text-red-300">
            {emailError}
          </p>
        )}
      </div>

      <div className="space-y-1.5">
        <div className="flex items-baseline justify-between">
          <Label htmlFor="password">Password</Label>
          {/* Password reset arrives with M1 ticket c; placeholder kept honest. */}
          <span className="text-xs text-neutral-500 dark:text-neutral-400">required</span>
        </div>
        <Input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          aria-invalid={passwordError ? true : undefined}
          aria-describedby={passwordError ? "password-error" : undefined}
          disabled={submitting}
          required
        />
        {passwordError && (
          <p id="password-error" className="text-xs text-red-700 dark:text-red-300">
            {passwordError}
          </p>
        )}
      </div>

      <Button type="submit" className="w-full" disabled={submitting}>
        {submitting ? "Signing in…" : "Sign in"}
      </Button>

      <p className="text-center text-sm text-neutral-600 dark:text-neutral-400">
        Don&apos;t have an account?{" "}
        <Link
          href={nextTarget === null ? "/register" : `/register?next=${encodeURIComponent(nextTarget)}`}
          className="font-medium text-neutral-900 underline underline-offset-2 dark:text-neutral-100"
        >
          Register
        </Link>
      </p>
    </form>
  );
}
