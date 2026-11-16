"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect } from "react";

// A distributor's own page moved into the main Automatic Distributors page: the drop down at the top opens it.
export default function DistributorRedirect() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  useEffect(() => {
    const id = Number(params.id);
    router.replace(Number.isInteger(id) ? `/admin/distributors/autodist?d=${id}` : "/admin/distributors/autodist");
  }, [params.id, router]);
  return <p className="text-sm text-gray-500">Loading…</p>;
}
