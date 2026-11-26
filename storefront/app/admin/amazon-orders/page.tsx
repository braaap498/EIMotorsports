"use client";

import AmazonOrders from "../../../components/admin/AmazonOrders";

export default function AmazonOrdersPage() {
  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold">Amazon: Orders</h1>
      <AmazonOrders />
    </div>
  );
}
