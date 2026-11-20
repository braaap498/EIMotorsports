import { Suspense } from "react";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { Metadata } from "next";
import { SITE_URL, jsonLdString, plainText, robotsMeta } from "../../../lib/site";
import ProductGallery from "../../../components/site/ProductGallery";
import AddToCart from "../../../components/site/AddToCart";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

type Detail = {
  id: number;
  slug: string;
  available: number;
  stock_mode?: "available" | "count";
  backordered?: boolean;
  expected_date?: string | null;
  days_until?: number | null;
  title: string;
  price: string | null;
  condition: string | null;
  part_number: string | null;
  sku: string;
  description: string | null;
  photos: { url: string; thumb: string; fallback: string | null }[];
  specifics: { name: string; value: string }[];
};

type Search = Promise<{ year?: string; model_id?: string }>;

// One fetch shared by the page and generateMetadata (same URL and options, so Next sends it once).
async function getProduct(slug: string): Promise<{ status: number; product: Detail | null }> {
  try {
    const res = await fetch(`${API}/store/fitment/product/${encodeURIComponent(slug)}`, { cache: "no-store" });
    if (!res.ok) return { status: res.status, product: null };
    return { status: res.status, product: (await res.json()) as Detail };
  } catch {
    return { status: 0, product: null };
  }
}

// Unknown or unpublished slug: a real 404 and noindex. Anything else is a metadata-only fallback.
export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }): Promise<Metadata> {
  const { slug } = await params;
  const { product: p } = await getProduct(slug);
  if (!p) return { title: "Part not found", robots: { index: false, follow: false } };

  // Every link to this product, with or without ?model_id=&year=, points at one address.
  const url = `/shop/${p.slug}`;
  const description =
    plainText(p.description, 150) || `${p.title}.${p.price ? ` $${p.price}.` : ""} Order from E&I Motorsports.`;
  const image = p.photos[0]?.url;
  return {
    title: p.title,
    description,
    alternates: { canonical: url },
    robots: robotsMeta(true),
    openGraph: { title: p.title, description, url, type: "website", ...(image ? { images: [{ url: image }] } : {}) },
  };
}

function conditionUrl(condition: string | null): string | undefined {
  const c = (condition ?? "").toLowerCase();
  if (!c) return undefined;
  if (c.includes("refurb")) return "https://schema.org/RefurbishedCondition";
  if (c.includes("used")) return "https://schema.org/UsedCondition";
  if (c.startsWith("new")) return "https://schema.org/NewCondition";
  return undefined;
}

// Product markup for Google (price, stock, condition). Only facts the page already shows.
function productSchema(p: Detail) {
  const url = `${SITE_URL}/shop/${p.slug}`;
  const images = p.photos.map((x) => x.url).filter(Boolean);
  const availability =
    p.available > 0
      ? "https://schema.org/InStock"
      : p.backordered
        ? "https://schema.org/BackOrder"
        : "https://schema.org/OutOfStock";
  const cond = conditionUrl(p.condition);
  return {
    "@context": "https://schema.org",
    "@type": "Product",
    name: p.title,
    sku: p.sku,
    ...(p.part_number ? { mpn: p.part_number } : {}),
    ...(images.length ? { image: images } : {}),
    description: plainText(p.description, 500) || p.title,
    url,
    ...(p.price
      ? {
          offers: {
            "@type": "Offer",
            url,
            priceCurrency: "USD",
            price: p.price,
            availability,
            ...(cond ? { itemCondition: cond } : {}),
          },
        }
      : {}),
  };
}

// Goes back to the parts list this product was opened from. If the page was
// opened directly (no model in the link), it falls back to the search.
async function BackLink({ searchParams }: { searchParams: Search }) {
  const { year, model_id } = await searchParams;
  const toResults = Boolean(model_id);
  const href = toResults
    ? `/shop?model_id=${encodeURIComponent(model_id!)}${year ? `&year=${encodeURIComponent(year)}` : ""}`
    : "/";
  return (
    <Link href={href} className="text-sm text-gray-600 underline">
      &larr; {toResults ? "Back to parts" : "Back to search"}
    </Link>
  );
}

async function Product({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const { status, product: p } = await getProduct(slug);
  if (status === 404) notFound();
  if (!p) {
    return <p className="text-red-600">Could not load this part right now.</p>;
  }

  return (
    <div className="mt-4 grid gap-8 md:grid-cols-2">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: jsonLdString(productSchema(p)) }} />
      <ProductGallery key={p.sku} photos={p.photos} title={p.title} />

      <div>
        <h1 className="text-2xl font-bold">{p.title}</h1>
        {p.price && <p className="mt-2 text-2xl">${p.price}</p>}
        <AddToCart productId={p.id} slug={p.slug} price={p.price} available={p.available} stockMode={p.stock_mode} backordered={p.backordered} expectedDate={p.expected_date} daysUntil={p.days_until} />
        <p className="mt-2 text-sm text-gray-500">
          {p.part_number ? `Part # ${p.part_number} · ` : ""}SKU {p.sku}
        </p>
        {p.condition && <p className="text-sm text-gray-500">Condition: {p.condition}</p>}

        {p.specifics.length > 0 && (
          <dl className="mt-6 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            {p.specifics.map((s) => (
              <div key={s.name} className="contents">
                <dt className="font-medium">{s.name}</dt>
                <dd>{s.value}</dd>
              </div>
            ))}
          </dl>
        )}

        {p.description && (
          <div
            className="product-description mt-6 text-sm"
            dangerouslySetInnerHTML={{ __html: p.description }}
          />
        )}
      </div>
    </div>
  );
}

export default function ProductPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Search;
}) {
  return (
    <main className="mx-auto max-w-5xl p-6">
      <Suspense
        fallback={<span className="text-sm text-gray-600">&larr; Back</span>}
      >
        <BackLink searchParams={searchParams} />
      </Suspense>
      <Suspense fallback={<p className="mt-4 text-gray-600">Loading...</p>}>
        <Product params={params} />
      </Suspense>
    </main>
  );
}
