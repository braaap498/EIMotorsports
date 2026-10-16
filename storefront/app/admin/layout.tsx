import type { Metadata } from "next";
import { Suspense, type ReactNode } from "react";
import AdminShell from "../../components/admin/AdminShell";

// Keep the back office out of search engines.
export const metadata: Metadata = {
  title: "E&I Admin",
  robots: { index: false, follow: false },
};

// The shell and pages use hooks that read the current URL (usePathname,
// useParams). Next.js needs a Suspense boundary around them so the build
// doesn't try to pre-render them with an unknown URL.
export default function AdminLayout({ children }: { children: ReactNode }) {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-gray-500">Loading…</p>}>
      <AdminShell>{children}</AdminShell>
    </Suspense>
  );
}
