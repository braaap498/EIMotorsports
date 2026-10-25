"use client";

import { useState } from "react";
import Link from "next/link";
import { MAX_LINE_QTY, useCart } from "./CartProvider";
import { backorderText } from "../../lib/backorder";

// Quantity picker and "Add to cart" button for a product page.
export default function AddToCart({
  productId,
  slug,
  price,
  available,
  backordered = false,
  expectedDate = null,
  daysUntil = null,
}: {
  productId: number;
  slug: string;
  price: string | null;
  available: number;
  backordered?: boolean;
  expectedDate?: string | null;
  daysUntil?: number | null;
}) {
  const { lines, add } = useCart();
  const inCart = lines.find((l) => l.product_id === productId)?.qty ?? 0;
  const max = Math.min(available, MAX_LINE_QTY);
  const room = Math.max(0, max - inCart); // how many more can be added
  const [qty, setQty] = useState(1);
  const [note, setNote] = useState<{ kind: "added" | "full"; text: string } | null>(null);

  if (!price) {
    return <p className="mt-6 text-sm font-semibold text-gray-600">Call or message us for pricing on this part.</p>;
  }
  if (available <= 0) {
    // Never a negative number or an "oversold" message: just sold out, or Backordered when more are on order.
    return backordered ? (
      <div className="mt-6">
        <p className="inline-block rounded bg-amber-50 px-3 py-2 text-sm font-semibold text-amber-800">Backordered</p>
        <p className="mt-2 text-sm text-gray-600">More are on order but haven’t arrived yet. {backorderText(expectedDate, daysUntil)}</p>
      </div>
    ) : (
      <p className="mt-6 inline-block rounded bg-gray-100 px-3 py-2 text-sm font-semibold text-gray-700">Out of stock</p>
    );
  }

  const clamp = (n: number) => Math.max(1, Math.min(Number.isFinite(n) ? Math.floor(n) : 1, Math.max(room, 1)));

  function submit() {
    if (room <= 0) {
      setNote({ kind: "full", text: `You already have all ${max} in your cart.` });
      return;
    }
    const added = add({ product_id: productId, slug }, clamp(qty), max);
    setQty(1);
    setNote({ kind: "added", text: added === 1 ? "1 added to your cart." : `${added} added to your cart.` });
  }

  const stepBtn = "flex h-10 w-10 items-center justify-center text-lg font-bold text-gray-800 hover:bg-gray-100 disabled:opacity-40";

  return (
    <div className="mt-6">
      <p className={`mb-2 text-sm font-semibold ${available <= 5 ? "text-amber-700" : "text-green-700"}`}>
        {available <= 5 ? `Only ${available} left` : "In stock"}
      </p>

      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center rounded border border-gray-300">
          <button type="button" className={stepBtn} onClick={() => setQty((q) => clamp(q - 1))} disabled={qty <= 1} aria-label="Fewer">
            −
          </button>
          <input
            aria-label="Quantity"
            inputMode="numeric"
            className="h-10 w-12 border-x border-gray-300 text-center text-sm outline-none"
            value={qty}
            onChange={(e) => setQty(clamp(Number(e.target.value.replace(/\D/g, ""))))}
          />
          <button type="button" className={stepBtn} onClick={() => setQty((q) => clamp(q + 1))} disabled={qty >= room} aria-label="More">
            +
          </button>
        </div>
        <button
          type="button"
          onClick={submit}
          className="rounded bg-[#06bde7] px-6 py-2.5 text-sm font-extrabold uppercase tracking-wide text-[#04202b] hover:brightness-110"
        >
          Add to cart
        </button>
      </div>

      {inCart > 0 && !note && <p className="mt-2 text-sm text-gray-600">{inCart} already in your cart.</p>}
      {note && (
        <p className={`mt-3 text-sm ${note.kind === "added" ? "text-green-700" : "text-amber-700"}`} role="status">
          {note.text}{" "}
          <Link href="/cart" className="font-semibold underline">View cart</Link>
        </p>
      )}
    </div>
  );
}
