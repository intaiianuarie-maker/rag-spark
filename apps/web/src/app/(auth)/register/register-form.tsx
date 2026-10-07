"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { auth, type AuthFailure } from "@/lib/auth-client";

const PASSWORD_MIN_LENGTH = 8; // mirrors backend PASSWORD_MIN_LENGTH (M1-001a)

function passwordHint(password: string): string | null {
  if (password.length === 0) return null;
  if (password.length < PASSWORD_MIN_LENGTH) {
    return `At least ${PASSWORD_MIN_LENGTH} characters.`;
  }
  return null;
}

/** Registration form (M1-002a). Success → panel with a link to /login
 * (register deliberately does not auto-login; flows stay decoupled). */
export function RegisterForm({ nextTarget }: { nextTarget: string | null }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [failure, setFailure] = useState<AuthFailure | null>(null);
  const [succeeded, setSucceeded] = useState(false);

  const confirmError =
    confirm.length > 0 && confirm !== password ? "Passwords do not match." : null;
  const localPasswordHint = passwordHint(password);
  const fieldErrors = failure?.fields;
  const emailError = fieldErrors?.email?.[0];
  const passwordError = fieldErrors?.password?.[0] ?? localPasswordHint;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    if (password.length < PASSWORD_MIN_LENGTH) {
      setFailure({
        code: "validation_error",
        message: `Please review the highlighted fields.`,
        status: 422,
        fields: { password: [`At least ${PASSWORD_MIN_LENGTH} characters.`] },
      });
      return;
    }
    if (confirm !== password) {
      setFailure({
        code: "validation_error",
        message: "Please review the highlighted fields.",
        status: 422,
        fields: { confirm: ["Passwords do not match."] },
      });
      return;
    }
    setFailure(null);
    setSubmitting(true);
    try {
      const result = await auth.register(email, password);
      if (result.ok) {
        setSucceeded(true);
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

  if (succeeded) {
    return (
      <div
        role="status"
        className="rounded-xl border border-emerald-600/30 bg-emerald-50 p-5 text-center dark:border-emerald-400/30 dark:bg-emerald-950/40"
      >
        <h2 className="text-sm font-semibold text-emerald-900 dark:text-emerald-200">
          Account created
        </h2>
        <p className="mt-2 text-sm text-emerald-900/80 dark:text-emerald-200/80">
          Continue to sign in with your new account.
        </p>
        <Button asChild className="mt-4 w-full">
          {nextTarget === null ? (
            <Link href="/login">Sign in</Link>
          ) : (
            <Link href={`/login?next=${encodeURIComponent(nextTarget)}`}>Sign in</Link>
          )}
        </Button>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4" noValidate>
      {failure !== null && (
        <div
          role="alert"
          className="rounded-lg border border-red-600/30 bg-red-50 p-3 text-sm text-red-900 dark:border-red-400/30 dark:bg-red-950/40 dark:text-red-200"
        >
          <p>{failure.message}</p>
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
        <Label htmlFor="password">Password</Label>
        <Input
          id="password"
          name="password"
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          aria-invalid={passwordError ? true : undefined}
          aria-describedby={passwordError ? "password-hint" : undefined}
          disabled={submitting}
          required
        />
        {passwordError && (
          <p id="password-hint" className="text-xs text-red-700 dark:text-red-300">
            {passwordError}
          </p>
        )}
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="confirm">Confirm password</Label>
        <Input
          id="confirm"
          name="confirm"
          type="password"
          autoComplete="new-password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          aria-invalid={confirmError ? true : undefined}
          aria-describedby={confirmError ? "confirm-error" : undefined}
          disabled={submitting}
          required
        />
        {confirmError && (
          <p id="confirm-error" className="text-xs text-red-700 dark:text-red-300">
            {confirmError}
          </p>
        )}
      </div>

      <Button type="submit" className="w-full" disabled={submitting}>
        {submitting ? "Creating account…" : "Create account"}
      </Button>

      <p className="text-center text-sm text-neutral-600 dark:text-neutral-400">
        Already have an account?{" "}
        <Link
          href={nextTarget === null ? "/login" : `/login?next=${encodeURIComponent(nextTarget)}`}
          className="font-medium text-neutral-900 underline underline-offset-2 dark:text-neutral-100"
        >
          Sign in
        </Link>
      </p>
    </form>
  );
}
