import { redirect } from "next/navigation";
import { cookies } from "next/headers";
import { ACCESS_COOKIE } from "../(auth)/api/auth/_lib/cookies";

/**
 * Knowledge shell (TASK-M2-002a). Server-side auth gate mirrors
 * /settings: anonymous visitors go to /login?next=… and resume back here.
 */
export const instant = false;

export default async function KnowledgeLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const jar = await cookies();
  if (!jar.get(ACCESS_COOKIE)?.value) {
    redirect(`/login?next=${encodeURIComponent("/knowledge")}`);
  }

  return (
    <div className="mx-auto w-full max-w-4xl flex-1 px-6 py-8">{children}</div>
  );
}