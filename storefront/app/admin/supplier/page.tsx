"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

// The Autodist page moved to Distributors > Automatic Distributors. Old bookmarks land here.
export default function SupplierRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/admin/distributors/autodist");
  }, [router]);
  return <p className="text-sm text-gray-500">Loading…</p>;
}
