import Link from "next/link";

/**
 * Auth shell for /login and /register (TASK-M1-002a). Centered narrow column;
 * inherits the root header/footer, which is fine for M1 (marked for review).
 */
export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex flex-1 flex-col items-center justify-center px-6 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <Link
            href="/"
            className="inline-flex items-center gap-2 text-lg font-semibold tracking-tight hover:opacity-80"
          >
            <span className="flex h-6 w-6 items-center justify-center rounded-md bg-neutral-900 text-[10px] font-bold text-white dark:bg-white dark:text-neutral-900">
              KO
            </span>
            Knowledge OS
          </Link>
          <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
            Sign in to your instance
          </p>
        </div>
        {children}
      </div>
    </main>
  );
}
