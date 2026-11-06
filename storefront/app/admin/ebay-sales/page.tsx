"use client";

import EbaySalesPanel from "../../../components/admin/EbaySalesPanel";

/* eBay orders: the list of recent orders. (Checking stores for new orders lives on Store API > Stores.) */
export default function EbayOrdersPage() {
  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold">Orders</h1>
      <EbaySalesPanel />
    </div>
  );
}
