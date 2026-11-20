import { Suspense } from "react";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { Metadata } from "next";
import ProductImage from "../../../../components/site/ProductImage";
import { SITE_URL, jsonLdString, robotsMeta, yearRanges } from "../../../../lib/site";
import { getVehicle } from "../../../../lib/seoApi";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";
const PAGE_SIZE = 24;

type Params = Promise<{ make: string; model: string }>;
type Search = Promise<{ page?: string }>;
type Item = {
  id: number;
  title: string;
  slug: string;
  price: string | null;
  universal: boolean;
  image_url: string | null;
  image_fallback: string | null;
};

const pageNum = (p: string | undefined) => Math.max(1, Number(p) || 1);

export async function generateMetadata({ params, searchParams }: { params: Params; searchParams: Search }): Promise<Metadata> {
  const { make, model } = await params;
  const page = pageNum((await searchParams).page);
  const { vehicle: v } = await getVehicle(make, model);
  if (!v) return { title: "Vehicle not found", robots: { index: false, follow: false } };

  const name = `${v.make.name} ${v.model.name}`;
  const years = yearRanges(v.years);
  // A submodel's address shows the same parts as the model it hangs under, so it names that page as canonical.
  const base = `/parts/${v.make.slug}/${v.canonical_model_slug}`;
  return {
    title: page > 1 ? `${name} Parts - Page ${page}` : `${name} Parts`,
    description: `Shop ${v.products} ${name} part${v.products === 1 ? "" : "s"}${years ? ` for ${years} models` : ""} at E&I Motorsports.`,
    alternates: { canonical: page > 1 ? `${base}?page=${page}` : base },
    robots: robotsMeta(true),
  };
}

async function VehicleParts({ params, searchParams }: { params: Params; searchParams: Search }) {
  const { make, model } = await params;
  const page = pageNum((await searchParams).page);
  const { status, vehicle: v } = await getVehicle(make, model);
  if (status === 404) notFound();
  if (!v) return <p className="text-red-600">Could not load this vehicle right now. Please try again.</p>;

  const qs = new URLSearchParams({ model_id: String(v.model.id), limit: String(PAGE_SIZE), offset: String((page - 1) * PAGE_SIZE) });
  let items: Item[] = [];
  let total = 0;
  let error = "";
  try {
    const res = await fetch(`${API}/store/fitment/products?${qs}`, { cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    items = data.items ?? [];
    total = data.total ?? 0;
  } catch {
    error = "Could not load parts right now. Please try again.";
  }

  const name = `${v.make.name} ${v.model.name}`;
  const years = yearRanges(v.years);
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const base = `/parts/${v.make.slug}/${v.canonical_model_slug}`;
  const pageHref = (n: number) => (n > 1 ? `${base}?page=${n}` : base);

  const crumbs = {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: [
      { "@type": "ListItem", position: 1, name: "Parts", item: `${SITE_URL}/parts` },
      { "@type": "ListItem", position: 2, name: v.make.name, item: `${SITE_URL}/parts/${v.make.slug}` },
      { "@type": "ListItem", position: 3, name: v.model.name, item: `${SITE_URL}${base}` },
    ],
  };

  return (
    <>
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: jsonLdString(crumbs) }} />
      <nav aria-label="Breadcrumb" className="text-sm text-gray-600">
        <Link href="/parts" className="underline">Parts</Link> /{" "}
        <Link href={`/parts/${v.make.slug}`} className="underline">{v.make.name}</Link> / {v.model.name}
      </nav>
      <h1 className="mb-1 mt-2 text-2xl font-bold">{name} Parts</h1>
      <p className="mb-4 text-sm text-gray-600">
        {total} part{total === 1 ? "" : "s"} that fit the {name}
        {years ? ` (${years})` : ""}.
      </p>

      {error && <p className="text-red-600">{error}</p>}
      {!error && items.length === 0 && <p className="text-gray-600">No parts found.</p>}

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
        {items.map((it) => (
          <Link key={it.id} href={`/shop/${it.slug}`} className="rounded border border-gray-200 p-3 hover:shadow">
            {it.image_url && (
              <ProductImage
                src={it.image_url}
                fallback={it.image_fallback}
                alt={it.title}
                className="mb-2 aspect-square w-full object-cover"
              />
            )}
            <p className="text-sm font-medium">{it.title}</p>
            {it.price != null && <p className="text-sm text-gray-700">${it.price}</p>}
            {it.universal && <p className="mt-1 text-xs font-semibold text-[#0489ad]">Universal fit</p>}
          </Link>
        ))}
      </div>

      {pages > 1 && (
        <div className="mt-6 flex items-center gap-4 text-sm">
          {page > 1 && <Link href={pageHref(page - 1)} className="underline">&larr; Previous</Link>}
          <span className="text-gray-600">Page {page} of {pages}</span>
          {page < pages && <Link href={pageHref(page + 1)} className="underline">Next &rarr;</Link>}
        </div>
      )}
    </>
  );
}

export default function VehiclePage({ params, searchParams }: { params: Params; searchParams: Search }) {
  return (
    <main className="mx-auto max-w-6xl p-6">
      <Suspense fallback={<p className="text-gray-600">Loading…</p>}>
        <VehicleParts params={params} searchParams={searchParams} />
      </Suspense>
    </main>
  );
}
