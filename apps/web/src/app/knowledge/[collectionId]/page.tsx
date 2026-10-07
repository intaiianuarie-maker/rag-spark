import type { Metadata } from "next";
import { CollectionDocuments } from "./documents-list";

export const metadata: Metadata = {
  title: "Documents",
};

export default async function CollectionDocumentsPage({
  params,
}: {
  params: Promise<{ collectionId: string }>;
}) {
  const { collectionId } = await params;
  return <CollectionDocuments collectionId={collectionId} />;
}