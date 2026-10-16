import type { Metadata } from "next";
import type { ReactNode } from "react";
import AdminShell from "../../components/admin/AdminShell";

// Keep the back office out of search engines.
export const metadata: Metadata = {
  title: "E&I Admin",
  robots: { index: false, follow: false },
};

export default function AdminLayout({ children }: { children: ReactNode }) {
  return <AdminShell>{children}</AdminShell>;
}
