"use client";

import AmazonListings from "../../../components/admin/AmazonListings";

export default function AmazonListingsPage() {
  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold">Amazon: Listings</h1>
      <AmazonListings />
    </div>
  );
}
