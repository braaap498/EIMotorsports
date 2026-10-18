"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice } from "../../components/admin/ui";
import { useAccess } from "../../components/admin/AdminAccess";

type Stats = { products: number; noFitment: number; untyped: number };

export default function AdminDashboard() {
  const { can, isAdmin } = useAccess();
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([
      api<{ total: number }>("/products?limit=1"),
      api<{ total: number }>("/products?no_fitment=true&limit=1"),
      api<{ total: number }>("/models?untyped=true&limit=1"),
    ])
      .then(([all, nofit, untyped]) =>
        setStats({ products: all.total, noFitment: nofit.total, untyped: untyped.total })
      )
      .catch((e) => setError(errMsg(e)));
  }, []);

  const allCards = [
    { label: "Products", value: stats?.products, href: "/admin/products", hint: "View all" },
    {
      label: "Products with no fitment",
      value: stats?.noFitment,
      href: "/admin/products?no_fitment=1",
      hint: "Not universal and not tied to any machine",
    },
    {
      label: "Models with no type",
      value: stats?.untyped,
      href: "/admin/models?untyped=1",
      hint: "Assign a machine type",
    },
  ];

  // "Models with no type" leads to an admin-only screen.
  const cards = allCards.filter((c) => isAdmin || c.href.startsWith("/admin/products"));

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Dashboard</h1>
      {error && <Notice kind="error">{error}</Notice>}
      <div className="grid gap-4 sm:grid-cols-3">
        {cards.map((c) => (
          <Link key={c.label} href={c.href} className="rounded-lg border border-gray-200 bg-white p-4 hover:border-gray-400">
            <div className="text-3xl font-semibold">{c.value ?? "…"}</div>
            <div className="mt-1 text-sm font-medium">{c.label}</div>
            <div className="text-xs text-gray-500">{c.hint}</div>
          </Link>
        ))}
      </div>
      <div className="flex gap-3">
        {can("products.create") && (
          <Link href="/admin/products/new" className="rounded bg-black px-4 py-2 text-sm font-semibold text-white">
            Add product
          </Link>
        )}
        {isAdmin && (
          <Link href="/admin/models" className="rounded border border-gray-300 bg-white px-4 py-2 text-sm font-medium">
            Manage models &amp; years
          </Link>
        )}
      </div>
    </div>
  );
}
