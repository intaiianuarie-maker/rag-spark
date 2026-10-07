import type { Metadata } from "next";
import { MembersClient } from "./members-client";

export const metadata: Metadata = {
  title: "Members",
};

export default function MembersPage() {
  return <MembersClient />;
}
