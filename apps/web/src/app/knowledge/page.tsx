import type { Metadata } from "next";
import { CollectionsList } from "./collections-list";

export const metadata: Metadata = {
  title: "Knowledge",
};

export default function KnowledgePage() {
  return <CollectionsList />;
}