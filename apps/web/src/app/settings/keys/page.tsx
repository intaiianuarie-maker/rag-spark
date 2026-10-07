import type { Metadata } from "next";
import { KeysClient } from "./keys-client";

export const metadata: Metadata = {
  title: "API keys",
};

export default function KeysPage() {
  return <KeysClient />;
}
