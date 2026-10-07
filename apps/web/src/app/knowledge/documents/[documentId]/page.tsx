import type { Metadata } from "next";
import { DocumentDetailView } from "./document-detail";

export const metadata: Metadata = {
  title: "Document",
};

export default async function DocumentDetailPage({
  params,
}: {
  params: Promise<{ documentId: string }>;
}) {
  const { documentId } = await params;
  return <DocumentDetailView documentId={documentId} />;
}