"use client";

import { useState } from "react";
import { storeApi } from "../../lib/storeApi";
import { backorderText } from "../../lib/backorder";

type Item = {
  title: string;
  quantity: number;
  unit_price_cents: number;
  backordered: boolean;
  backordered_units: number;
  expected_date: string | null;
  days_until: number | null;
};
type Tracked = {
  order_number: string;
  status: string;
  status_label: string;
  paid_at: string | null;
  shipped_at: string | null;
  tracking_number: string | null;
  carrier: string | null;
  tracking_url: string | null;
  total_cents: number;
  refunded_cents: number;
  ship_to: { name: string | null; city: string | null; region: string | null };
  has_backorder: boolean;
  items: Item[];
};

const money = (c: number) => `$${(c / 100).toFixed(2)}`;
const day = (iso: string | null) =>
  iso ? new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : null;
const inputCls = "w-full rounded border border-gray-300 px-3 py-2 text-sm";

export default function TrackOrder() {
  const [orderNumber, setOrderNumber] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [order, setOrder] = useState<Tracked | null>(null);

  async function lookup(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setOrder(null);
    try {
      setOrder(
        await storeApi<Tracked>("/orders/track", {
          method: "POST",
          body: { order_number: orderNumber.trim(), email: email.trim() },
        })
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "We couldn’t look that order up. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  const shipped = order && (order.status === "shipped" || !!order.shipped_at);

  return (
    <main className="mx-auto max-w-2xl p-6">
      <h1 className="mb-3 text-2xl font-bold">Track your order</h1>
      <p className="mb-4 text-sm text-gray-600">
        Enter your order number and the email you used at checkout. You can find both in your confirmation email.
      </p>

      <form onSubmit={lookup} className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
        <label className="block text-sm font-medium">
          Order number
          <input className={`${inputCls} mt-1`} value={orderNumber} onChange={(e) => setOrderNumber(e.target.value)} required />
        </label>
        <label className="block text-sm font-medium">
          Email
          <input className={`${inputCls} mt-1`} type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </label>
        <button
          disabled={busy}
          className="rounded bg-[#06bde7] px-6 py-2.5 text-sm font-extrabold uppercase tracking-wide text-[#04202b] hover:brightness-110 disabled:opacity-60"
        >
          {busy ? "Looking…" : "Track order"}
        </button>
      </form>

      {error && <p className="mt-4 text-sm font-semibold text-red-700">{error}</p>}

      {order && (
        <section className="mt-6 space-y-4 rounded-lg border border-gray-200 bg-white p-4">
          <div>
            <p className="text-sm text-gray-500">Order {order.order_number}</p>
            <p className="text-xl font-bold">{order.status_label}</p>
            {order.paid_at && <p className="text-sm text-gray-600">Paid {day(order.paid_at)}</p>}
          </div>

          {shipped && (
            <div className="rounded bg-green-50 p-3 text-sm text-green-900">
              <p className="font-semibold">Shipped{order.shipped_at ? ` ${day(order.shipped_at)}` : ""}</p>
              {order.tracking_number ? (
                <p className="mt-1">
                  {order.carrier ? `${order.carrier} · ` : ""}Tracking number{" "}
                  {order.tracking_url ? (
                    <a href={order.tracking_url} target="_blank" rel="noreferrer" className="font-semibold underline">
                      {order.tracking_number}
                    </a>
                  ) : (
                    <span className="font-semibold">{order.tracking_number}</span>
                  )}
                </p>
              ) : (
                <p className="mt-1">A tracking number will appear here soon.</p>
              )}
            </div>
          )}

          {order.has_backorder && (
            <p className="rounded bg-amber-50 p-3 text-sm text-amber-900">
              Part of your order is backordered. We’ll ship as soon as it arrives.
            </p>
          )}

          <ul className="divide-y divide-gray-100 text-sm">
            {order.items.map((i, n) => (
              <li key={n} className="py-2">
                <div className="flex justify-between gap-4">
                  <span>
                    {i.title} <span className="text-gray-500">× {i.quantity}</span>
                  </span>
                  <span className="whitespace-nowrap">{money(i.unit_price_cents * i.quantity)}</span>
                </div>
                {i.backordered && (
                  <p className="mt-1 text-xs font-semibold text-amber-800">
                    Backordered. {backorderText(i.expected_date, i.days_until)}
                  </p>
                )}
              </li>
            ))}
          </ul>

          <div className="flex justify-between border-t border-gray-200 pt-3 text-sm font-semibold">
            <span>Total</span>
            <span>{money(order.total_cents)}</span>
          </div>
          {order.refunded_cents > 0 && <p className="text-sm text-gray-600">Refunded {money(order.refunded_cents)}</p>}
        </section>
      )}
    </main>
  );
}
