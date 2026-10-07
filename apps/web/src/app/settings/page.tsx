import { redirect } from "next/navigation";

/** /settings → default section. */
export default function SettingsIndex() {
  redirect("/settings/members");
}
