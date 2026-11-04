"use client";

import { useAccess } from "../../../components/admin/AdminAccess";
import { Notice } from "../../../components/admin/ui";
import DistributorList from "../../../components/admin/Distributors";

// Every distributor you order from. Opens to each distributor's own page.
export default function DistributorsPage() {
  const { can } = useAccess();
  if (!can("inventory.purchase")) {
    return <Notice kind="error">Your account doesn’t have access to distributors.</Notice>;
  }
  return <DistributorList />;
}
