"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCart } from "../../../components/site/CartProvider";
import { storeApi } from "../../../lib/storeApi";

type Order = {
  order_number: string;
  status: string;
  email: string;
  subtotal_cents: number;
  shipping_cents: number;
  tax_cents: number;
  total_cents: number;
  shipping_method: string | null;
  ship_to: { name: string | null; city: string | null; region: string | null };
  items: { title: string; unit_price_cents: number; quantity: number; line_total_cents: number }[];
};

const money = (c: number) => `$${(c / 100).toFixed(2)}`;
const wrap = "mx-auto max-w-3xl px-4 py-10";

function Confirmation() {
  const sessionId = useSearchParams().get("session_id");
  const { clear } = useCart();
  const [order, setOrder] = useState<Order | null>(null);
  const [notFound, setNotFound] = useState(false);
  const cleared = useRef(false);

  // Stripe's confirmation can arrive a few seconds after the customer returns,
  // so check again for a short while until the order shows as paid.
  useEffect(() => {
    if (!sessionId) {
      setNotFound(true);
      return;
    }
    let cancelled = false;
    let tries = 0;
    const check = () => {
      storeApi<Order>(`/checkout/order?session_id=${encodeURIComponent(sessionId)}`)
        .then((o) => {
          if (cancelled) return;
          setOrder(o);
          if (o.status === "pending_payment" && ++tries < 15) setTimeout(check, 2000);
        })
        .catch(() => !cancelled && setNotFound(true));
    };
    check();
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  const paid = !!order && ["paid", "shipped", "delivered"].includes(order.status);
  useEffect(() => {
    if (paid && !cleared.current) {
      cleared.current = true;
      clear(); // the order is paid, so the cart is no longer needed
    }
  }, [paid, clear]);

  if (notFound) {
    return (
      <main className={wrap}>
        <h1 className="text-3xl font-extrabold text-gray-900">We couldn't find that order</h1>
        <p className="mt-3 text-gray-600">If you just paid, give it a minute and refresh this page.</p>
        <Link href="/" className="mt-5 inline-block text-sm text-gray-700 underline">Back to the store</Link>
      </main>
    );
  }
  if (!order) return <main className={`${wrap} text-gray-600`}>Loading your order…</main>;

  if (order.status === "cancelled") {
    return (
      <main className={wrap}>
        <h1 className="text-3xl font-extrabold text-gray-900">This order was not completed</h1>
        <p className="mt-3 text-gray-600">You were not charged. Your cart is still saved.</p>
        <Link href="/cart" className="mt-5 inline-block text-sm text-gray-700 underline">Back to your cart</Link>
      </main>
    );
  }

  return (
    <main className={wrap}>
      <h1 className="text-3xl font-extrabold text-gray-900">{paid ? "Thank you for your order!" : "Confirming your payment…"}</h1>
      <p className="mt-2 text-gray-700">
        Order <span className="font-semibold">{order.order_number}</span>
        {paid ? "" : ". This usually takes a few seconds. You can refresh this page."}
      </p>
      {paid && <p className="mt-1 text-sm text-gray-600">A receipt is sent to {order.email} by our payment provider.</p>}

      <section className="mt-6 rounded-lg border border-gray-200 bg-white p-5">
        <ul className="divide-y divide-gray-100 text-sm">
          {order.items.map((i, n) => (
            <li key={n} className="flex justify-between gap-3 py-2">
              <span className="text-gray-800">{i.title} <span className="text-gray-500">× {i.quantity}</span></span>
              <span className="shrink-0 text-gray-900">{money(i.line_total_cents)}</span>
            </li>
          ))}
        </ul>
        <dl className="mt-3 space-y-1 border-t border-gray-200 pt-3 text-sm">
          <div className="flex justify-between"><dt className="text-gray-600">Subtotal</dt><dd>{money(order.subtotal_cents)}</dd></div>
          <div className="flex justify-between">
            <dt className="text-gray-600">Shipping{order.shipping_method ? ` (${order.shipping_method})` : ""}</dt>
            <dd>{order.shipping_cents === 0 ? "Free" : money(order.shipping_cents)}</dd>
          </div>
          {order.tax_cents > 0 && <div className="flex justify-between"><dt className="text-gray-600">Sales tax</dt><dd>{money(order.tax_cents)}</dd></div>}
          <div className="flex justify-between border-t border-gray-200 pt-2 text-base font-bold">
            <dt>Total</dt><dd>{money(order.total_cents)}</dd>
          </div>
        </dl>
        {order.ship_to.name && (
          <p className="mt-4 text-sm text-gray-600">
            Shipping to {order.ship_to.name}, {order.ship_to.city}, {order.ship_to.region}
          </p>
        )}
      </section>

      <Link href="/" className="mt-6 inline-block rounded bg-[#06bde7] px-5 py-2.5 text-sm font-bold text-[#04202b] hover:brightness-110">
        Keep shopping
      </Link>
    </main>
  );
}

export default function ConfirmationPage() {
  return (
    <Suspense fallback={<main className={`${wrap} text-gray-600`}>Loading…</main>}>
      <Confirmation />
    </Suspense>
  );
}
