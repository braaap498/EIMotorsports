"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import VehicleSelector from "../VehicleSelector";
import { useAuth } from "./AuthProvider";
import GarageMenu from "./GarageMenu";
import { useCart } from "./CartProvider";
import ProductImage from "./ProductImage";
import { CHECKOUT_ENABLED } from "../../lib/features";
import { CartIcon, ChevronIcon, SearchIcon, UserIcon } from "./icons";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

const SHOW_CART = true;

// What the store says about a cart line right now (same answer the cart page gets).
type Priced = {
  product_id: number;
  slug?: string;
  title?: string;
  price?: string | null;
  image_url?: string | null;
  image_fallback?: string | null;
  problem: null | "unavailable" | "sold_out" | "no_price" | "reduced";
};
const money = (c: number) => `$${(c / 100).toFixed(2)}`;

// product_count is optional: once /store/categories returns it (products in
// that category itself), empty branches are hidden automatically.
type Cat = {
  id: number;
  parent_id: number | null;
  name: string;
  slug: string;
  sort_order: number;
  product_count?: number;
};
type Node = Cat & { children: Node[]; total: number };

const bySort = (a: Cat, b: Cat) => a.sort_order - b.sort_order || a.name.localeCompare(b.name);

function buildTree(all: Cat[]): Node[] {
  const byParent = new Map<number | null, Cat[]>();
  for (const c of all) {
    const list = byParent.get(c.parent_id) ?? [];
    list.push(c);
    byParent.set(c.parent_id, list);
  }
  const hasCounts = all.some((c) => typeof c.product_count === "number");
  const make = (c: Cat): Node => {
    const kids = (byParent.get(c.id) ?? []).sort(bySort).map(make);
    const children = hasCounts ? kids.filter((k) => k.total > 0) : kids;
    const total = (c.product_count ?? 0) + children.reduce((s, k) => s + k.total, 0);
    return { ...c, children, total };
  };
  const roots = (byParent.get(null) ?? []).sort(bySort).map(make);
  return hasCounts ? roots.filter((r) => r.total > 0) : roots;
}

export default function SiteHeader() {
  const pathname = usePathname();
  const sp = useSearchParams();
  const { user, ready, isStaff, garage, openAuth, signOut, requireCheckoutChoice } = useAuth();
  const router = useRouter();
  const { count: cartCount, lines } = useCart();
  const [cats, setCats] = useState<Cat[]>([]);
  const [menu, setMenu] = useState(false);
  const [rideOpen, setRideOpen] = useState(false);
  const [openId, setOpenId] = useState<number | null>(null); // desktop mega-menu
  const [catsOpen, setCatsOpen] = useState(false); // mobile category list
  const menuRef = useRef<HTMLDivElement>(null); // the account menu, for outside-click detection
  // What the last pointer was ("mouse", "touch", "pen"). Hover menus only react to a real mouse;
  // checking the pointer itself works on laptops with touch screens, where the screen-wide test fails.
  const lastPointer = useRef<string>("mouse");
  const track = (e: ReactPointerEvent) => {
    lastPointer.current = e.pointerType;
  };
  const [cartMenu, setCartMenu] = useState(false); // cart preview on hover
  const [preview, setPreview] = useState<Priced[] | null>(null);

  useEffect(() => {
    fetch(`${API}/store/categories`)
      .then((r) => (r.ok ? r.json() : []))
      .then((all: Cat[]) => setCats(all))
      .catch(() => {});
  }, []);

  const tree = useMemo(() => buildTree(cats), [cats]);

  // Close menus whenever the page or filters change.
  const search = sp.toString();
  useEffect(() => {
    setOpenId(null);
    setCatsOpen(false);
    setMenu(false);
    setCartMenu(false);
  }, [pathname, search]);

  // Close the account menu on a click or tap outside it, or on Escape.
  useEffect(() => {
    if (!menu) return;
    const onDown = (e: MouseEvent | TouchEvent) => {
      const t = e.target;
      if (menuRef.current && t instanceof Element && !menuRef.current.contains(t)) setMenu(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenu(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("touchstart", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("touchstart", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  // Cart preview: look up today's prices when the preview opens.
  const cartKey = lines.map((l) => `${l.product_id}x${l.qty}`).join(",");
  useEffect(() => {
    if (!cartMenu || lines.length === 0) return;
    let cancelled = false;
    fetch(`${API}/store/cart/price`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: lines.map((l) => ({ product_id: l.product_id, qty: l.qty })) }),
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d: { items: Priced[] }) => !cancelled && setPreview(d.items))
      .catch(() => !cancelled && setPreview([]));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cartMenu, cartKey]);

  // The back office has its own layout.
  if (pathname.startsWith("/admin")) return null;

  // Keep the chosen machine when browsing a category.
  const year = sp.get("year");
  const modelId = sp.get("model_id");
  const ride = year && modelId ? `&year=${encodeURIComponent(year)}&model_id=${encodeURIComponent(modelId)}` : "";
  // Staff accounts have a display name ("Eric"); everyone else falls back to the part of the email before the @.
  const displayName = (user?.user_metadata as { display_name?: string } | undefined)?.display_name?.trim();
  const name = displayName || user?.email?.split("@")[0] || "";
  const href = (slug: string) => `/search?category=${encodeURIComponent(slug)}${ride}`;
  const active = tree.find((c) => c.id === openId && c.children.length > 0) ?? null;

  const previewRows = lines.map((line) => {
    const p = preview?.find((x) => x.product_id === line.product_id);
    const ok = !!p && (p.problem === null || p.problem === "reduced") && p.price != null;
    return { line, p, ok, unit: ok ? Math.round(parseFloat(p?.price ?? "0") * 100) : 0 };
  });
  const previewSubtotal = previewRows.reduce((sum, r) => sum + r.unit * r.line.qty, 0);
  const previewBlocked = preview !== null && preview.length > 0 && previewRows.some((r) => !r.ok);
  function goCheckout() {
    setCartMenu(false);
    requireCheckoutChoice((choice) => router.push(`/checkout?as=${choice}`));
  }

  return (
    <header className="sticky top-0 z-30 shadow-md">
      {/* Row 1: logo, search, account */}
      <div className="bg-black">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-3 px-4 py-3">
          <Link href="/" aria-label="E&I Motorsports - home" className="flex shrink-0 items-center gap-3">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="/ei-mark.png" alt="" className="h-11 w-auto" />
            <span className="hidden text-lg font-extrabold tracking-[0.28em] text-white sm:block">MOTORSPORTS</span>
          </Link>

          <form action="/search" method="get" role="search" className="order-last flex w-full min-w-0 md:order-none md:w-auto md:flex-1">
            <input
              name="q"
              type="search"
              placeholder="Enter keyword or part #"
              className="min-w-0 flex-1 rounded-l bg-white px-4 py-2.5 text-sm text-gray-900 outline-none placeholder:text-gray-500"
            />
            <button className="rounded-r bg-[#06bde7] px-4 text-[#04202b] hover:brightness-110" aria-label="Search">
              <SearchIcon className="h-5 w-5" />
            </button>
          </form>

          <nav className="ml-auto flex items-center gap-5 text-sm font-semibold text-white md:ml-0">
            <Link href="/about" className="hidden hover:text-[#06bde7] md:block">About</Link>
            <Link href="/help" className="hidden hover:text-[#06bde7] md:block">Help</Link>
            <Link href="/track-order" className="hidden hover:text-[#06bde7] md:block">Track Order</Link>

            {!ready ? (
              <span className="h-8 w-20" />
            ) : !user ? (
              <button type="button" onClick={() => openAuth("signin")} className="flex items-center gap-2 hover:text-[#06bde7]">
                Sign in <UserIcon className="h-7 w-7" />
              </button>
            ) : (
              <div
                ref={menuRef}
                className="relative"
                // Mouse: open on hover. Touch screens have no hover, so a tap opens and closes it.
                onPointerEnter={(e) => {
                  track(e);
                  if (e.pointerType === "mouse") setMenu(true);
                }}
                onPointerLeave={(e) => e.pointerType === "mouse" && setMenu(false)}
                onPointerDown={track}
              >
                <button
                  type="button"
                  onClick={() => setMenu((m) => (lastPointer.current === "mouse" ? true : !m))}
                  className="flex items-center gap-2 hover:text-[#06bde7]"
                  aria-expanded={menu}
                  aria-haspopup="menu"
                >
                  <span className="hidden max-w-32 truncate sm:block">Hi, {name}</span>
                  <UserIcon className="h-7 w-7" />
                  <ChevronIcon className={`h-3.5 w-3.5 transition ${menu ? "rotate-180" : ""}`} />
                </button>
                {menu && (
                  // pt-2 (not a margin) keeps the pointer inside the menu while moving down to it
                  <div className="absolute right-0 z-50 pt-2">
                    <div className="w-64 overflow-hidden rounded-lg bg-white text-sm font-medium text-gray-900 shadow-xl">
                      <div className="border-b border-gray-200 bg-gray-50 px-4 py-3">
                        <p className="truncate font-bold">{name}</p>
                        {user.email && <p className="truncate text-xs font-normal text-gray-600">{user.email}</p>}
                        <p className="mt-1 text-xs font-normal text-gray-600">
                          {garage.length === 0
                            ? "No machines in your garage yet"
                            : `${garage.length} ${garage.length === 1 ? "machine" : "machines"} in your garage`}
                        </p>
                      </div>
                      <div className="py-1">
                        <Link href="/account" onClick={() => setMenu(false)} className="block px-4 py-2 hover:bg-gray-100">My account</Link>
                        <Link href="/cart" onClick={() => setMenu(false)} className="flex items-center justify-between px-4 py-2 hover:bg-gray-100">
                          Cart
                          {cartCount > 0 && <span className="text-xs font-normal text-gray-600">{cartCount} {cartCount === 1 ? "item" : "items"}</span>}
                        </Link>
                        <Link href="/track-order" onClick={() => setMenu(false)} className="block px-4 py-2 hover:bg-gray-100">Track Order</Link>
                        {isStaff && (
                          <Link href="/admin" onClick={() => setMenu(false)} className="block px-4 py-2 hover:bg-gray-100">Admin</Link>
                        )}
                      </div>
                      <div className="border-t border-gray-200 py-1">
                        <button
                          type="button"
                          onClick={() => {
                            setMenu(false);
                            signOut();
                          }}
                          className="block w-full px-4 py-2 text-left hover:bg-gray-100"
                        >
                          Sign out
                        </button>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )}

            {SHOW_CART && (
              <div
                className="relative"
                // Hover (mouse) or keyboard focus shows the cart contents. Touch screens go straight to the cart page.
                onPointerEnter={(e) => {
                  track(e);
                  if (e.pointerType === "mouse") setCartMenu(true);
                }}
                onPointerLeave={(e) => e.pointerType === "mouse" && setCartMenu(false)}
                onPointerDown={track}
                onFocus={() => lastPointer.current === "mouse" && setCartMenu(true)}
                onBlur={(e) => {
                  const next = e.relatedTarget;
                  if (!(next instanceof Element) || !e.currentTarget.contains(next)) setCartMenu(false);
                }}
                onKeyDown={(e) => e.key === "Escape" && setCartMenu(false)}
              >
                <Link href="/cart" aria-label={`Cart, ${cartCount} item${cartCount === 1 ? "" : "s"}`} className="relative block hover:text-[#06bde7]">
                  <CartIcon className="h-8 w-8" />
                  {cartCount > 0 && (
                    <span className="absolute -right-2 -top-2 flex h-5 min-w-5 items-center justify-center rounded-full bg-[#06bde7] px-1 text-[11px] font-extrabold text-[#04202b]">
                      {cartCount > 99 ? "99+" : cartCount}
                    </span>
                  )}
                </Link>
                {cartMenu && cartCount === 0 && (
                  <div className="absolute right-0 z-50 pt-2">
                    <div className="w-56 rounded-lg bg-white px-4 py-3 text-sm font-medium text-gray-900 shadow-xl">
                      Your cart is empty.
                    </div>
                  </div>
                )}
                {cartMenu && cartCount > 0 && (
                  // pt-2 (not a margin) keeps the pointer inside while moving down to the card
                  <div className="absolute right-0 z-50 pt-2">
                    <div className="w-80 overflow-hidden rounded-lg bg-white text-sm font-medium text-gray-900 shadow-xl">
                      <ul className="max-h-80 divide-y divide-gray-100 overflow-y-auto">
                        {previewRows.slice(0, 5).map(({ line, p, ok, unit }) => (
                          <li key={line.product_id} className="flex gap-3 px-4 py-3">
                            <Link
                              href={`/shop/${p?.slug ?? line.slug}`}
                              onClick={() => setCartMenu(false)}
                              className="h-12 w-12 shrink-0 overflow-hidden rounded border border-gray-200 bg-gray-50"
                            >
                              {p?.image_url ? (
                                <ProductImage src={p.image_url} fallback={p.image_fallback} alt="" className="h-full w-full object-cover" />
                              ) : null}
                            </Link>
                            <div className="min-w-0 flex-1">
                              <p className="line-clamp-2 text-xs font-semibold leading-snug">
                                {p?.title ?? (preview === null ? "Loading…" : "This part")}
                              </p>
                              {p && !ok ? (
                                <p className="text-xs font-normal text-red-700">Unavailable</p>
                              ) : (
                                <p className="text-xs font-normal text-gray-600">{line.qty} × {ok ? money(unit) : "…"}</p>
                              )}
                            </div>
                            {ok && <span className="shrink-0 text-xs font-semibold">{money(unit * line.qty)}</span>}
                          </li>
                        ))}
                      </ul>
                      {previewRows.length > 5 && (
                        <p className="border-t border-gray-100 px-4 py-2 text-xs font-normal text-gray-600">
                          + {previewRows.length - 5} more in your cart
                        </p>
                      )}
                      <div className="border-t border-gray-200 bg-gray-50 px-4 py-3">
                        <div className="mb-3 flex justify-between">
                          <span>Subtotal ({cartCount} {cartCount === 1 ? "item" : "items"})</span>
                          <span className="font-bold">{preview === null || preview.length === 0 ? "…" : money(previewSubtotal)}</span>
                        </div>
                        <div className="flex gap-2">
                          <Link
                            href="/cart"
                            onClick={() => setCartMenu(false)}
                            className="flex-1 rounded border border-gray-300 bg-white px-3 py-2 text-center text-sm font-bold hover:bg-gray-100"
                          >
                            View cart
                          </Link>
                          {CHECKOUT_ENABLED && (
                            <button
                              type="button"
                              onClick={goCheckout}
                              disabled={preview === null || previewBlocked}
                              className="flex-1 rounded bg-[#06bde7] px-3 py-2 text-sm font-extrabold text-[#04202b] hover:brightness-110 disabled:opacity-40"
                            >
                              Checkout
                            </button>
                          )}
                        </div>
                        {previewBlocked && <p className="mt-2 text-xs font-normal text-red-700">Review your cart first: something is unavailable.</p>}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )}
          </nav>
        </div>
      </div>

      {/* Row 2: fitment */}
      <div className="bg-[#0b2b3a]">
        <div className="mx-auto max-w-7xl px-4 py-3 lg:flex lg:items-center lg:gap-6">
          <div className="flex items-center justify-between lg:block lg:w-44 lg:shrink-0">
            <button
              type="button"
              onClick={() => setRideOpen((o) => !o)}
              className="flex items-center gap-2 text-base font-bold text-white lg:pointer-events-none"
              aria-expanded={rideOpen}
            >
              Select Your Ride
              <ChevronIcon className={`h-4 w-4 transition lg:hidden ${rideOpen ? "rotate-180" : ""}`} />
            </button>
            <GarageMenu />
          </div>
          <div className={`${rideOpen ? "mt-3 block" : "hidden"} lg:mt-0 lg:block lg:flex-1`}>
            <VehicleSelector />
          </div>
        </div>
      </div>

      {/* Row 3: categories (mega-menu on desktop, accordion on mobile) */}
      {tree.length > 0 && (
        <div
          className="relative bg-[#06bde7] text-[#04202b]"
          onMouseLeave={() => setOpenId(null)}
          onKeyDown={(e) => e.key === "Escape" && setOpenId(null)}
        >
          {/* Desktop: top-level bar */}
          <nav className="mx-auto hidden max-w-7xl flex-nowrap justify-between px-2 lg:flex" aria-label="Categories">
            {tree.map((c) => (
              <Link
                key={c.id}
                href={href(c.slug)}
                onMouseEnter={() => setOpenId(c.children.length ? c.id : null)}
                onFocus={() => setOpenId(c.children.length ? c.id : null)}
                onClick={() => setOpenId(null)}
                aria-haspopup={c.children.length > 0}
                aria-expanded={openId === c.id}
                className={`whitespace-nowrap px-3 py-2 text-sm font-semibold hover:bg-black/10 ${openId === c.id ? "bg-black/10" : ""}`}
              >
                {c.name}
              </Link>
            ))}
          </nav>

          {/* Desktop: dropdown panel */}
          {active && (
            <div className="absolute inset-x-0 top-full z-40 hidden border-t border-black/10 bg-white text-gray-900 shadow-xl lg:block">
              <div className="mx-auto max-w-7xl px-6 py-5">
                <Link
                  href={href(active.slug)}
                  onClick={() => setOpenId(null)}
                  className="mb-4 block border-b-2 border-[#06bde7] pb-1.5 text-base font-bold hover:text-[#0489ad]"
                >
                  Shop all {active.name}
                </Link>
                <div className="columns-3 gap-10 text-sm">
                  {active.children.map((ch) => (
                    <div key={ch.id} className="mb-2 break-inside-avoid">
                      <Link
                        href={href(ch.slug)}
                        onClick={() => setOpenId(null)}
                        className={`block py-1.5 hover:text-[#0489ad] hover:underline ${ch.children.length ? "font-bold" : ""}`}
                      >
                        {ch.name}
                      </Link>
                      {ch.children.map((g) => (
                        <Link
                          key={g.id}
                          href={href(g.slug)}
                          onClick={() => setOpenId(null)}
                          className="block py-1 pl-3 text-gray-600 hover:text-[#0489ad] hover:underline"
                        >
                          {g.name}
                        </Link>
                      ))}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* Mobile: one button, then an accordion */}
          <div className="lg:hidden">
            <button
              type="button"
              onClick={() => setCatsOpen((o) => !o)}
              aria-expanded={catsOpen}
              className="flex w-full items-center justify-between px-4 py-2.5 text-sm font-semibold"
            >
              Shop categories
              <ChevronIcon className={`h-4 w-4 transition ${catsOpen ? "rotate-180" : ""}`} />
            </button>
            {catsOpen && (
              <div className="max-h-[65vh] overflow-y-auto bg-white text-sm text-gray-900">
                {tree.map((c) =>
                  c.children.length === 0 ? (
                    <Link key={c.id} href={href(c.slug)} className="block border-t border-gray-100 px-4 py-3 font-semibold">
                      {c.name}
                    </Link>
                  ) : (
                    <details key={c.id} className="border-t border-gray-100">
                      <summary className="cursor-pointer list-none px-4 py-3 font-semibold">{c.name}</summary>
                      <div className="bg-gray-50 pb-2">
                        <Link href={href(c.slug)} className="block px-6 py-2 font-medium text-[#0489ad]">
                          Shop all {c.name}
                        </Link>
                        {c.children.map((ch) => (
                          <div key={ch.id}>
                            <Link href={href(ch.slug)} className="block px-6 py-2">{ch.name}</Link>
                            {ch.children.map((g) => (
                              <Link key={g.id} href={href(g.slug)} className="block px-10 py-1.5 text-gray-600">
                                {g.name}
                              </Link>
                            ))}
                          </div>
                        ))}
                      </div>
                    </details>
                  )
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </header>
  );
}
