import Link from "next/link";
import { redirect } from "next/navigation";
import { cookies } from "next/headers";
import { ACCESS_COOKIE } from "../(auth)/api/auth/_lib/cookies";

/**
 * Settings shell (TASK-M1-002b). Server-side auth gate: an anonymous visitor
 * is redirected to /login?next=<target> (resume via ?next on the way back).
 * Blocking (cacheComponents `instant = false`): the gate must run before any
 * settings content renders.
 */
export const instant = false;

export default async function SettingsLayout({ children }: { children: React.ReactNode }) {
  const jar = await cookies();
  if (!jar.get(ACCESS_COOKIE)?.value) {
    const target = "/settings/members";
    redirect(`/login?next=${encodeURIComponent(target)}`);
  }

  return (
    <div className="mx-auto w-full max-w-3xl flex-1 px-6 py-8">
      <header className="mb-6">
        <h1 className="text-xl font-semibold tracking-tight">Workspace settings</h1>
        <nav aria-label="Settings sections" className="mt-3 flex gap-4 text-sm">
          <Link
            href="/settings/members"
            className="rounded-md px-2 py-1 font-medium hover:bg-neutral-100 dark:hover:bg-neutral-800"
          >
            Members
          </Link>
          <Link
            href="/settings/keys"
            className="rounded-md px-2 py-1 font-medium hover:bg-neutral-100 dark:hover:bg-neutral-800"
          >
            API keys
          </Link>
        </nav>
      </header>
      {children}
    </div>
  );
}
