"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import VehicleSelector from "../VehicleSelector";
import { useAuth } from "./AuthProvider";
import GarageMenu from "./GarageMenu";
import { useCart } from "./CartProvider";
import { AtvIcon, ChevronIcon, SearchIcon, UserIcon } from "./icons";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

const SHOW_CART = true;

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
  const { user, ready, isStaff, openAuth, signOut } = useAuth();
  const { count: cartCount } = useCart();
  const [cats, setCats] = useState<Cat[]>([]);
  const [menu, setMenu] = useState(false);
  const [rideOpen, setRideOpen] = useState(false);
  const [openId, setOpenId] = useState<number | null>(null); // desktop mega-menu
  const [catsOpen, setCatsOpen] = useState(false); // mobile category list

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
  }, [pathname, search]);

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
            <Link href="/track-order" className="hidden hover:text-[#06bde7] md:block">Track Order</Link>

            {!ready ? (
              <span className="h-8 w-20" />
            ) : !user ? (
              <button type="button" onClick={() => openAuth("signin")} className="flex items-center gap-2 hover:text-[#06bde7]">
                Sign in <UserIcon className="h-7 w-7" />
              </button>
            ) : (
              <div className="relative">
                <button type="button" onClick={() => setMenu((m) => !m)} className="flex items-center gap-2 hover:text-[#06bde7]" aria-expanded={menu}>
                  <span className="hidden max-w-32 truncate sm:block">Hi, {name}</span>
                  <UserIcon className="h-7 w-7" />
                  <ChevronIcon className="h-3.5 w-3.5" />
                </button>
                {menu && (
                  <>
                    <button type="button" aria-label="Close menu" className="fixed inset-0 z-40 cursor-default" onClick={() => setMenu(false)} />
                    <div className="absolute right-0 z-50 mt-2 w-48 overflow-hidden rounded-lg bg-white py-1 text-sm font-medium text-gray-900 shadow-xl">
                      <Link href="/account" onClick={() => setMenu(false)} className="block px-4 py-2 hover:bg-gray-100">My account</Link>
                      <Link href="/track-order" onClick={() => setMenu(false)} className="block px-4 py-2 hover:bg-gray-100">Track Order</Link>
                      {isStaff && (
                        <Link href="/admin" onClick={() => setMenu(false)} className="block px-4 py-2 hover:bg-gray-100">Admin</Link>
                      )}
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
                  </>
                )}
              </div>
            )}

            {SHOW_CART && (
              <Link href="/cart" aria-label={`Cart, ${cartCount} item${cartCount === 1 ? "" : "s"}`} className="relative hover:text-[#06bde7]">
                <AtvIcon className="h-8 w-12" />
                {cartCount > 0 && (
                  <span className="absolute -right-2 -top-2 flex h-5 min-w-5 items-center justify-center rounded-full bg-[#06bde7] px-1 text-[11px] font-extrabold text-[#04202b]">
                    {cartCount > 99 ? "99+" : cartCount}
                  </span>
                )}
              </Link>
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
