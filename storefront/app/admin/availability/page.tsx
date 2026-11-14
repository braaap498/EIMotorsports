"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

// Availability moved: its settings and drop ship ticks are on the Drop ship page, and the website display is under Products.
export default function AvailabilityMoved() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/admin/dropship");
  }, [router]);
  return <p className="p-8 text-sm text-gray-500">Moved to Drop ship…</p>;
}
