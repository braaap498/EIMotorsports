"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { MAX_LINE_QTY, useCart } from "../../components/site/CartProvider";
import { useAuth } from "../../components/site/AuthProvider";
import ProductImage from "../../components/site/ProductImage";
import { CHECKOUT_ENABLED } from "../../lib/features";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

type Priced = {
  product_id: number;
  slug?: string;
  sku?: string;
  title?: string;
  price?: string | null;
  available?: number;
  backordered?: boolean;
  qty?: number;
  image_url?: string | null;
  image_fallback?: string | null;
  problem: null | "unavailable" | "sold_out" | "no_price" | "reduced";
};

const cents = (price: string | null | undefined) => Math.round(parseFloat(price ?? "0") * 100);
const money = (c: number) => `$${(c / 100).toFixed(2)}`;

export default function CartPage() {
  const router = useRouter();
  const { lines, loaded, setQty, remove, clear } = useCart();
  const { requireCheckoutChoice } = useAuth();
  const [priced, setPriced] = useState<Priced[] | null>(null);
  const [error, setError] = useState("");

  // Ask the store for today's price and stock whenever the cart changes.
  const key = lines.map((l) => `${l.product_id}x${l.qty}`).join(",");
  useEffect(() => {
    if (!loaded) return;
    if (lines.length === 0) {
      setPriced([]);
      return;
    }
    let cancelled = false;
    fetch(`${API}/store/cart/price`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: lines.map((l) => ({ product_id: l.product_id, qty: l.qty })) }),
    })
      .then((r) => {
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      })
      .then((d: { items: Priced[] }) => {
        if (cancelled) return;
        setPriced(d.items);
        setError("");
        // If stock is lower than what was in the cart, bring the cart down to match.
        for (const it of d.items) {
          const mine = lines.find((l) => l.product_id === it.product_id);
          if (mine && it.problem === "reduced" && it.qty && it.qty !== mine.qty) setQty(it.product_id, it.qty);
        }
      })
      .catch(() => !cancelled && setError("Could not check prices right now. Please try again."));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, key]);

  const rows = useMemo(
    () =>
      lines.map((l) => {
        const p = priced?.find((x) => x.product_id === l.product_id);
        const buyable = !!p && (p.problem === null || p.problem === "reduced") && p.price != null;
        return { line: l, p, buyable, unit: cents(p?.price) };
      }),
    [lines, priced]
  );
  const buyableRows = rows.filter((r) => r.buyable);
  const subtotal = buyableRows.reduce((sum, r) => sum + r.unit * r.line.qty, 0);
  const blocked = rows.some((r) => priced && !r.buyable);
  const pieces = buyableRows.reduce((n, r) => n + r.line.qty, 0);

  function checkout() {
    requireCheckoutChoice((choice) => router.push(`/checkout?as=${choice}`));
  }

  if (!loaded || (lines.length > 0 && priced === null && !error)) {
    return <main className="mx-auto max-w-5xl px-4 py-10 text-gray-600">Loading your cart…</main>;
  }

  if (lines.length === 0) {
    return (
      <main className="mx-auto max-w-5xl px-4 py-10">
        <h1 className="text-3xl font-extrabold text-gray-900">Your cart</h1>
        <p className="mt-3 text-gray-600">Your cart is empty.</p>
        <Link href="/" className="mt-5 inline-block rounded bg-[#06bde7] px-5 py-2.5 text-sm font-bold text-[#04202b] hover:brightness-110">
          Shop parts
        </Link>
      </main>
    );
  }

  const stepBtn = "flex h-8 w-8 items-center justify-center text-base font-bold text-gray-800 hover:bg-gray-100 disabled:opacity-40";

  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <div className="mb-6 flex items-end justify-between gap-4">
        <h1 className="text-3xl font-extrabold text-gray-900">Your cart</h1>
        <button type="button" onClick={clear} className="text-sm text-gray-600 underline">Empty cart</button>
      </div>

      {error && <p className="mb-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">{error}</p>}

      <div className="grid gap-8 lg:grid-cols-[1fr_20rem]">
        <ul className="divide-y divide-gray-200 rounded-lg border border-gray-200 bg-white">
          {rows.map(({ line, p, buyable, unit }) => (
            <li key={line.product_id} className="flex gap-4 p-4">
              <Link href={`/shop/${p?.slug ?? line.slug}`} className="h-24 w-24 shrink-0 overflow-hidden rounded border border-gray-200 bg-gray-50">
                {p?.image_url ? (
                  <ProductImage src={p.image_url} fallback={p.image_fallback} alt="" className="h-full w-full object-cover" />
                ) : null}
              </Link>

              <div className="min-w-0 flex-1">
                <Link href={`/shop/${p?.slug ?? line.slug}`} className="font-medium text-gray-900 hover:underline">
                  {p?.title ?? "This part"}
                </Link>
                {p?.sku && <p className="text-xs text-gray-500">SKU {p.sku}</p>}

                {p && p.problem === "unavailable" && <p className="mt-1 text-sm font-semibold text-red-700">This part is no longer available.</p>}
                {p && p.problem === "sold_out" && (
                  <p className="mt-1 text-sm font-semibold text-red-700">
                    {p.backordered ? "Backordered. More are on order, but none are in stock right now." : "Sold out."}
                  </p>
                )}
                {p && p.problem === "no_price" && <p className="mt-1 text-sm font-semibold text-red-700">Pricing unavailable. Contact us to order.</p>}
                {p && p.problem === "reduced" && <p className="mt-1 text-sm font-semibold text-amber-700">Only {p.available} in stock. We lowered your quantity.</p>}

                <div className="mt-3 flex flex-wrap items-center gap-4">
                  {buyable ? (
                    <div className="flex items-center rounded border border-gray-300">
                      <button type="button" className={stepBtn} aria-label="Fewer" disabled={line.qty <= 1} onClick={() => setQty(line.product_id, line.qty - 1)}>
                        −
                      </button>
                      <span className="w-10 border-x border-gray-300 text-center text-sm leading-8">{line.qty}</span>
                      <button
                        type="button"
                        className={stepBtn}
                        aria-label="More"
                        disabled={line.qty >= Math.min(p?.available ?? 1, MAX_LINE_QTY)}
                        onClick={() => setQty(line.product_id, line.qty + 1)}
                      >
                        +
                      </button>
                    </div>
                  ) : null}
                  <button type="button" onClick={() => remove(line.product_id)} className="text-sm text-red-700 underline">Remove</button>
                </div>
              </div>

              {buyable && (
                <div className="text-right text-sm">
                  <p className="font-semibold text-gray-900">{money(unit * line.qty)}</p>
                  {line.qty > 1 && <p className="text-gray-500">{money(unit)} each</p>}
                </div>
              )}
            </li>
          ))}
        </ul>

        <aside className="h-fit rounded-lg border border-gray-200 bg-white p-5">
          <h2 className="text-lg font-bold text-gray-900">Order summary</h2>
          <dl className="mt-4 space-y-2 text-sm">
            <div className="flex justify-between">
              <dt className="text-gray-600">Subtotal ({pieces} {pieces === 1 ? "item" : "items"})</dt>
              <dd className="font-semibold text-gray-900">{money(subtotal)}</dd>
            </div>
            <div className="flex justify-between text-gray-500">
              <dt>Shipping and tax</dt>
              <dd>At checkout</dd>
            </div>
          </dl>

          {blocked && <p className="mt-4 text-sm text-red-700">Remove the unavailable parts above to continue.</p>}

          {CHECKOUT_ENABLED ? (
            <button
              type="button"
              onClick={checkout}
              disabled={blocked || buyableRows.length === 0}
              className="mt-5 w-full rounded bg-[#06bde7] px-4 py-3 text-sm font-extrabold uppercase tracking-wide text-[#04202b] hover:brightness-110 disabled:opacity-40"
            >
              Checkout
            </button>
          ) : (
            <p className="mt-5 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              Online checkout is not open yet. Your cart is saved on this device.
            </p>
          )}
          <Link href="/help" className="mt-4 block text-center text-sm text-gray-600 underline">Shipping &amp; returns info</Link>
          <Link href="/" className="mt-2 block text-center text-sm text-gray-600 underline">Continue shopping</Link>
        </aside>
      </div>
    </main>
  );
}
