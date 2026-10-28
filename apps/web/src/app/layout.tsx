import Link from "next/link";
import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Geist, Geist_Mono } from "next/font/google";
import { WorkspaceProvider } from "@/components/workspace/workspace-provider";
import { WorkspaceSwitcher } from "@/components/workspace/switcher";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: {
    default: "AI Knowledge OS — Status",
    template: "%s — AI Knowledge OS",
  },
  description:
    "Self-hosted, local-first AI knowledge platform. M0 skeleton: system status page.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col bg-background text-foreground">
        <WorkspaceProvider>
          <header className="border-b border-neutral-200 dark:border-neutral-800">
            <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center justify-between gap-4 px-6 py-4">
              <Link href="/" className="flex items-center gap-2 font-semibold tracking-tight">
                <span className="flex h-6 w-6 items-center justify-center rounded-md bg-neutral-900 text-[10px] font-bold text-white dark:bg-white dark:text-neutral-900">
                  KO
                </span>
                Knowledge OS
              </Link>
              <div className="flex flex-wrap items-center gap-3">
                <nav aria-label="Primary" className="flex flex-wrap gap-y-2 text-sm text-neutral-500 dark:text-neutral-400">
                  <Link href="/ask" className="px-2 hover:text-neutral-800 dark:hover:text-neutral-200">Ask</Link>
                  <Link href="/search" className="px-2 hover:text-neutral-800 dark:hover:text-neutral-200">Search</Link>
                  <Link href="/knowledge" className="px-2 hover:text-neutral-800 dark:hover:text-neutral-200">Knowledge</Link>
                  <Link href="/sources" className="px-2 hover:text-neutral-800 dark:hover:text-neutral-200">Sources</Link>
                  <Link href="/eval" className="px-2 hover:text-neutral-800 dark:hover:text-neutral-200">Evaluation</Link>
                </nav>
                <WorkspaceSwitcher />
              </div>
            </div>
          </header>
          {children}
        </WorkspaceProvider>
        <footer className="border-t border-neutral-200 dark:border-neutral-800">
          <div className="mx-auto w-full max-w-5xl px-6 py-4 text-xs text-neutral-500 dark:text-neutral-400">
            AI Knowledge Operating System — M0 skeleton. Self-hosted, local-first.
          </div>
        </footer>
      </body>
    </html>
  );
}
