"use client";

import EbaySalesPanel from "../../../components/admin/EbaySalesPanel";

/* eBay sales: polling, set-aside and quantity sync. Moved here from the Live listings page; the panel is unchanged. */
export default function EbaySalesPage() {
  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold">Sales</h1>
      <EbaySalesPanel />
    </div>
  );
}
