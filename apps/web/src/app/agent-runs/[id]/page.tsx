import { Suspense } from "react";
import AgentRunDetailPage from "./run-detail";

// The run id is only available at request time (useParams in the client).
export const instant = false;

export default function Page() {
  return (
    <Suspense fallback={<p role="status">Loading agent run…</p>}>
      <AgentRunDetailPage />
    </Suspense>
  );
}
