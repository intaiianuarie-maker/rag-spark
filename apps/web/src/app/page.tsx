import { HealthPanel } from "@/components/status/health-panel";

/**
 * M0 status page (TASK-M0-003 requirement 3). The shell is a static server
 * component; the health data is fetched client-side via the /api/health proxy
 * so loading, error and empty states are explicit and testable.
 */
export default function StatusPage() {
  return (
    <main className="flex flex-1 items-center justify-center px-6 py-12">
      <HealthPanel />
    </main>
  );
}
