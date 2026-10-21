"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useCart } from "../../components/site/CartProvider";
import { CHECKOUT_ENABLED } from "../../lib/features";
import { storeApi } from "../../lib/storeApi";
import { getSupabase } from "../../lib/supabase";

type QuoteLine = {
  product_id: number;
  title?: string;
  sku?: string;
  qty?: number;
  unit_price_cents?: number;
  line_total_cents?: number;
  problem: string | null;
};
type ShipOption = { code: string; label: string; cents: number };
type Quote = {
  ok: boolean;
  items: QuoteLine[];
  subtotal_cents: number;
  shipping_options: ShipOption[];
  tax_cents: number | null;
};
type Saved = {
  id: number;
  kind: "shipping" | "billing";
  full_name: string;
  line1: string;
  line2: string | null;
  city: string;
  region: string;
  postal_code: string;
  country: string;
  phone: string | null;
  is_default: boolean;
};
type Addr = {
  full_name: string;
  line1: string;
  line2: string;
  city: string;
  region: string;
  postal_code: string;
  phone: string;
};

const money = (c: number) => `$${(c / 100).toFixed(2)}`;
const EMPTY: Addr = { full_name: "", line1: "", line2: "", city: "", region: "", postal_code: "", phone: "" };
const input = "w-full rounded border border-gray-300 px-3 py-2 text-sm text-gray-900";
const label = "mb-1 block text-sm font-medium text-gray-700";

export default function CheckoutPage() {
  const { lines, loaded } = useCart();
  const [quote, setQuote] = useState<Quote | null>(null);
  const [loadError, setLoadError] = useState("");
  const [signedIn, setSignedIn] = useState(false);
  const [email, setEmail] = useState("");
  const [saved, setSaved] = useState<Saved[]>([]);
  const [choice, setChoice] = useState<number | "new">("new");
  const [addr, setAddr] = useState<Addr>(EMPTY);
  const [shipCode, setShipCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [cartProblem, setCartProblem] = useState(false);

  const key = lines.map((l) => `${l.product_id}x${l.qty}`).join(",");

  // Today's prices, stock and shipping choices from the server (same check the cart uses).
  useEffect(() => {
    if (!CHECKOUT_ENABLED || !loaded || lines.length === 0) return;
    let cancelled = false;
    storeApi<Quote>("/checkout/quote", {
      method: "POST",
      body: { items: lines.map((l) => ({ product_id: l.product_id, qty: l.qty })) },
    })
      .then((q) => {
        if (cancelled) return;
        setQuote(q);
        setShipCode((cur) => cur || q.shipping_options[0]?.code || "");
      })
      .catch((e: unknown) => !cancelled && setLoadError(e instanceof Error ? e.message : "Could not load checkout."));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, key]);

  // Signed-in customers: prefill the email and offer their saved shipping addresses.
  useEffect(() => {
    if (!CHECKOUT_ENABLED) return;
    let cancelled = false;
    getSupabase()
      .auth.getSession()
      .then(async ({ data }) => {
        const user = data.session?.user;
        if (!user || cancelled) return;
        setSignedIn(true);
        setEmail((cur) => cur || user.email || "");
        try {
          const all = await storeApi<Saved[]>("/addresses");
          if (cancelled) return;
          const ship = all.filter((a) => a.kind === "shipping");
          setSaved(ship);
          const def = ship.find((a) => a.is_default) ?? ship[0];
          if (def) setChoice(def.id);
        } catch {
          /* saved addresses are optional: fall back to typing one */
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const option = useMemo(() => quote?.shipping_options.find((o) => o.code === shipCode), [quote, shipCode]);
  const subtotal = quote?.subtotal_cents ?? 0;
  const total = subtotal + (option?.cents ?? 0);

  function setField(k: keyof Addr, v: string) {
    setAddr((a) => ({ ...a, [k]: v }));
  }

  function chosenAddress() {
    if (choice !== "new") {
      const s = saved.find((a) => a.id === choice);
      if (s) {
        return {
          full_name: s.full_name,
          line1: s.line1,
          line2: s.line2 ?? "",
          city: s.city,
          region: s.region,
          postal_code: s.postal_code,
          country: s.country,
          phone: s.phone ?? "",
        };
      }
    }
    return { ...addr, country: "US" };
  }

  async function pay(e: FormEvent) {
    e.preventDefault();
    if (busy || !quote || !option) return;
    setBusy(true);
    setError("");
    setCartProblem(false);
    try {
      const res = await storeApi<{ url: string }>("/checkout/start", {
        method: "POST",
        body: {
          items: lines.map((l) => ({ product_id: l.product_id, qty: l.qty })),
          shipping_option: option.code,
          email: email.trim(),
          shipping_address: chosenAddress(),
        },
      });
      window.location.href = res.url; // Stripe's payment page; card details never touch this site
    } catch (err: unknown) {
      const status = (err as { status?: number })?.status;
      if (status === 409) setCartProblem(true);
      setError(err instanceof Error ? err.message : "Something went wrong. Please try again.");
      setBusy(false);
    }
  }

  const wrap = "mx-auto max-w-5xl px-4 py-10";

  if (!CHECKOUT_ENABLED) {
    return (
      <main className={wrap}>
        <h1 className="text-3xl font-extrabold text-gray-900">Checkout</h1>
        <p className="mt-3 text-gray-600">Online checkout is not open yet.</p>
        <Link href="/cart" className="mt-5 inline-block text-sm text-gray-700 underline">Back to your cart</Link>
      </main>
    );
  }
  if (!loaded) return <main className={`${wrap} text-gray-600`}>Loading…</main>;
  if (lines.length === 0) {
    return (
      <main className={wrap}>
        <h1 className="text-3xl font-extrabold text-gray-900">Checkout</h1>
        <p className="mt-3 text-gray-600">Your cart is empty.</p>
        <Link href="/" className="mt-5 inline-block text-sm text-gray-700 underline">Shop parts</Link>
      </main>
    );
  }
  if (loadError) {
    return (
      <main className={wrap}>
        <h1 className="text-3xl font-extrabold text-gray-900">Checkout</h1>
        <p className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">{loadError}</p>
        <Link href="/cart" className="mt-5 inline-block text-sm text-gray-700 underline">Back to your cart</Link>
      </main>
    );
  }
  if (!quote) return <main className={`${wrap} text-gray-600`}>Loading checkout…</main>;
  if (!quote.ok) {
    return (
      <main className={wrap}>
        <h1 className="text-3xl font-extrabold text-gray-900">Checkout</h1>
        <p className="mt-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          Something in your cart changed or is no longer available. Please review your cart first.
        </p>
        <Link href="/cart" className="mt-5 inline-block rounded bg-[#06bde7] px-5 py-2.5 text-sm font-bold text-[#04202b] hover:brightness-110">
          Review your cart
        </Link>
      </main>
    );
  }

  const usingSaved = choice !== "new";

  return (
    <main className={wrap}>
      <h1 className="mb-6 text-3xl font-extrabold text-gray-900">Checkout</h1>
      {!signedIn && (
        <p className="mb-4 text-sm text-gray-600">You are checking out as a guest. No account needed.</p>
      )}

      <form onSubmit={pay} className="grid gap-8 lg:grid-cols-[1fr_20rem]">
        <div className="space-y-6">
          <section className="rounded-lg border border-gray-200 bg-white p-5">
            <h2 className="text-lg font-bold text-gray-900">Contact</h2>
            <div className="mt-3">
              <label className={label} htmlFor="email">Email (for your receipt)</label>
              <input id="email" type="email" required autoComplete="email" className={input} value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
          </section>

          <section className="rounded-lg border border-gray-200 bg-white p-5">
            <h2 className="text-lg font-bold text-gray-900">Ship to</h2>

            {saved.length > 0 && (
              <div className="mt-3 space-y-2">
                {saved.map((a) => (
                  <label key={a.id} className="flex cursor-pointer gap-3 rounded border border-gray-200 p-3 text-sm">
                    <input type="radio" name="addr" checked={choice === a.id} onChange={() => setChoice(a.id)} />
                    <span className="text-gray-800">
                      <span className="font-semibold">{a.full_name}</span>
                      <br />
                      {a.line1}{a.line2 ? `, ${a.line2}` : ""}, {a.city}, {a.region} {a.postal_code}
                    </span>
                  </label>
                ))}
                <label className="flex cursor-pointer gap-3 rounded border border-gray-200 p-3 text-sm">
                  <input type="radio" name="addr" checked={choice === "new"} onChange={() => setChoice("new")} />
                  <span className="font-semibold text-gray-800">Use a different address</span>
                </label>
              </div>
            )}

            {!usingSaved && (
              <div className="mt-4 grid gap-3 sm:grid-cols-2">
                <div className="sm:col-span-2">
                  <label className={label} htmlFor="full_name">Full name</label>
                  <input id="full_name" required maxLength={100} autoComplete="name" className={input} value={addr.full_name} onChange={(e) => setField("full_name", e.target.value)} />
                </div>
                <div className="sm:col-span-2">
                  <label className={label} htmlFor="line1">Address</label>
                  <input id="line1" required maxLength={120} autoComplete="address-line1" className={input} value={addr.line1} onChange={(e) => setField("line1", e.target.value)} />
                </div>
                <div className="sm:col-span-2">
                  <label className={label} htmlFor="line2">Apartment, suite, etc. (optional)</label>
                  <input id="line2" maxLength={120} autoComplete="address-line2" className={input} value={addr.line2} onChange={(e) => setField("line2", e.target.value)} />
                </div>
                <div>
                  <label className={label} htmlFor="city">City</label>
                  <input id="city" required maxLength={80} autoComplete="address-level2" className={input} value={addr.city} onChange={(e) => setField("city", e.target.value)} />
                </div>
                <div>
                  <label className={label} htmlFor="region">State</label>
                  <input id="region" required maxLength={50} autoComplete="address-level1" placeholder="OH" className={input} value={addr.region} onChange={(e) => setField("region", e.target.value)} />
                </div>
                <div>
                  <label className={label} htmlFor="postal_code">ZIP code</label>
                  <input id="postal_code" required maxLength={12} autoComplete="postal-code" className={input} value={addr.postal_code} onChange={(e) => setField("postal_code", e.target.value)} />
                </div>
                <div>
                  <label className={label} htmlFor="phone">Phone (optional)</label>
                  <input id="phone" type="tel" maxLength={25} autoComplete="tel" className={input} value={addr.phone} onChange={(e) => setField("phone", e.target.value)} />
                </div>
              </div>
            )}
            <p className="mt-3 text-xs text-gray-500">We currently ship within the United States.</p>
          </section>

          <section className="rounded-lg border border-gray-200 bg-white p-5">
            <h2 className="text-lg font-bold text-gray-900">Shipping method</h2>
            <div className="mt-3 space-y-2">
              {quote.shipping_options.map((o) => (
                <label key={o.code} className="flex cursor-pointer items-center justify-between gap-3 rounded border border-gray-200 p-3 text-sm">
                  <span className="flex items-center gap-3">
                    <input type="radio" name="ship" checked={shipCode === o.code} onChange={() => setShipCode(o.code)} />
                    <span className="font-medium text-gray-900">{o.label}</span>
                  </span>
                  <span className="font-semibold text-gray-900">{o.cents === 0 ? "Free" : money(o.cents)}</span>
                </label>
              ))}
            </div>
          </section>
        </div>

        <aside className="h-fit rounded-lg border border-gray-200 bg-white p-5">
          <h2 className="text-lg font-bold text-gray-900">Order summary</h2>
          <ul className="mt-3 space-y-2 text-sm">
            {quote.items.map((l) => (
              <li key={l.product_id} className="flex justify-between gap-3">
                <span className="text-gray-700">{l.title} <span className="text-gray-500">× {l.qty}</span></span>
                <span className="shrink-0 text-gray-900">{money(l.line_total_cents ?? 0)}</span>
              </li>
            ))}
          </ul>
          <dl className="mt-4 space-y-2 border-t border-gray-200 pt-4 text-sm">
            <div className="flex justify-between">
              <dt className="text-gray-600">Subtotal</dt>
              <dd className="font-semibold text-gray-900">{money(subtotal)}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-gray-600">Shipping</dt>
              <dd className="font-semibold text-gray-900">{option ? (option.cents === 0 ? "Free" : money(option.cents)) : "-"}</dd>
            </div>
            {quote.tax_cents === null && (
              <div className="flex justify-between text-gray-500">
                <dt>Sales tax</dt>
                <dd>Calculated at payment</dd>
              </div>
            )}
            <div className="flex justify-between border-t border-gray-200 pt-2 text-base">
              <dt className="font-bold text-gray-900">Total</dt>
              <dd className="font-bold text-gray-900">{money(total)}</dd>
            </div>
          </dl>

          {error && (
            <p className="mt-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
              {error}{" "}
              {cartProblem && <Link href="/cart" className="font-semibold underline">Review your cart</Link>}
            </p>
          )}

          <button
            type="submit"
            disabled={busy || !option}
            className="mt-5 w-full rounded bg-[#06bde7] px-4 py-3 text-sm font-extrabold uppercase tracking-wide text-[#04202b] hover:brightness-110 disabled:opacity-40"
          >
            {busy ? "Taking you to payment…" : "Continue to payment"}
          </button>
          <p className="mt-3 text-center text-xs text-gray-500">You will enter your card on Stripe's secure page.</p>
          <Link href="/cart" className="mt-3 block text-center text-sm text-gray-600 underline">Back to cart</Link>
        </aside>
      </form>
    </main>
  );
}
