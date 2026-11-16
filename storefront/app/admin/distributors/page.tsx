"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

// The distributor list moved into the main Automatic Distributors page: pick a distributor in the drop down at the top.
export default function DistributorsRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/admin/distributors/autodist");
  }, [router]);
  return <p className="text-sm text-gray-500">Loading…</p>;
}
