"use client";

import { useAccess } from "../../../components/admin/AdminAccess";
import { Notice } from "../../../components/admin/ui";
import PurchaseOrders from "../../../components/admin/PurchaseOrders";

// The Purchase orders screen (reorder low / out-of-stock parts, distributors, PO history).
export default function PurchaseOrdersPage() {
  const { can } = useAccess();
  if (!can("inventory.purchase")) {
    return <Notice kind="error">Your account doesn’t have access to purchase orders.</Notice>;
  }
  return <PurchaseOrders />;
}
