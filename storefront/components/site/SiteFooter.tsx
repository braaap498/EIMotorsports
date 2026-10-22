"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

// Every link opens a tab of the Help page (/help#returns and so on).
const LINKS = [
  { href: "/help#shipping", label: "Shipping" },
  { href: "/help#returns", label: "Returns" },
  { href: "/help#refunds", label: "Refunds" },
  { href: "/help#privacy", label: "Privacy" },
  { href: "/help#contact", label: "Contact" },
];

export default function SiteFooter() {
  const pathname = usePathname();
  // The back office has its own layout and no footer.
  if (pathname.startsWith("/admin")) return null;

  return (
    <footer className="mt-10 bg-black text-sm text-gray-300">
      <div className="mx-auto max-w-6xl px-4 py-8">
        <nav aria-label="Store information" className="flex flex-wrap gap-x-6 gap-y-2">
          {LINKS.map((l) => (
            <Link key={l.href} href={l.href} className="hover:text-[#06bde7]">
              {l.label}
            </Link>
          ))}
        </nav>
        <p className="mt-4 text-gray-400">All prices are in US dollars (USD).</p>
        <p className="mt-1 text-gray-500">&copy; E&amp;I Motorsports</p>
      </div>
    </footer>
  );
}
