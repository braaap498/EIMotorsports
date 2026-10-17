"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import VehicleSelector from "../VehicleSelector";
import { useAuth } from "./AuthProvider";
import GarageMenu from "./GarageMenu";
import { CartIcon, ChevronIcon, SearchIcon, UserIcon } from "./icons";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

// Flip to true once the store has a cart page at /cart.
const SHOW_CART = false;

type Cat = { id: number; parent_id: number | null; name: string; slug: string; sort_order: number };

export default function SiteHeader() {
  const pathname = usePathname();
  const sp = useSearchParams();
  const { user, ready, isStaff, openAuth, signOut } = useAuth();
  const [cats, setCats] = useState<Cat[]>([]);
  const [menu, setMenu] = useState(false);
  const [rideOpen, setRideOpen] = useState(false);

  useEffect(() => {
    fetch(`${API}/store/categories`)
      .then((r) => (r.ok ? r.json() : []))
      .then((all: Cat[]) => setCats(all.filter((c) => c.parent_id === null)))
      .catch(() => {});
  }, []);

  // The back office has its own layout.
  if (pathname.startsWith("/admin")) return null;

  // Keep the chosen machine when browsing a category.
  const year = sp.get("year");
  const modelId = sp.get("model_id");
  const ride = year && modelId ? `&year=${encodeURIComponent(year)}&model_id=${encodeURIComponent(modelId)}` : "";
  const name = user?.email?.split("@")[0] ?? "";

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
              <Link href="/cart" aria-label="Cart" className="hover:text-[#06bde7]">
                <CartIcon className="h-7 w-7" />
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

      {/* Row 3: categories */}
      {cats.length > 0 && (
        <div className="bg-[#06bde7] text-[#04202b]">
          <nav className="mx-auto flex max-w-7xl overflow-x-auto px-2" aria-label="Categories">
            {cats.map((c) => (
              <Link
                key={c.id}
                href={`/search?category=${encodeURIComponent(c.slug)}${ride}`}
                className="whitespace-nowrap px-4 py-2 text-sm font-semibold hover:bg-black/10"
              >
                {c.name}
              </Link>
            ))}
          </nav>
        </div>
      )}
    </header>
  );
}
