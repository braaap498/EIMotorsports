import { Suspense } from "react";
import Link from "next/link";
import type { Metadata } from "next";
import { robotsMeta } from "../../lib/site";
import ProductImage from "../../components/site/ProductImage";

import { getJson } from "../../lib/cachedApi";

// Kept out of the index for now: the address is a number (?model_id=123), so it can't say which
// vehicle it is. Real make/model pages (/parts/polaris/ranger-xp-900) will replace it for search engines.
export const metadata: Metadata = {
  title: "Parts for your vehicle",
  robots: robotsMeta(false),
};

type Product = {
  id?: string | number;
  title?: string;
  name?: string;
  price?: number | string;
  image_url?: string;
  image_fallback?: string | null;
  slug?: string;
};

type Params = Promise<{ year?: string; model_id?: string }>;

async function Results({ searchParams }: { searchParams: Params }) {
  const { year, model_id } = await searchParams;

  let products: Product[] = [];
  let error = "";

  if (!model_id) {
    error = "Pick a make and model first.";
  } else {
    const yr = year ? `&year=${encodeURIComponent(year)}` : "";
    // Only plain numbers are remembered, so odd addresses cannot fill the cache.
    const plain = /^\d+$/.test(model_id) && (!year || /^\d{4}$/.test(year));
    const res = await getJson<unknown>(`/store/fitment/products?model_id=${encodeURIComponent(model_id)}${yr}`, { cache: plain });
    if (res.data) {
      const data = res.data as { items?: Product[]; products?: Product[] } | Product[];
      products = Array.isArray(data) ? data : data.items ?? data.products ?? [];
    } else {
      error = "Could not load parts right now. Please try again.";
    }
  }

  return (
    <>
      <h1 className="mb-4 mt-2 text-2xl font-bold">
        Parts that fit your {year ? `${year} ` : ""}vehicle{year ? "" : " (all years)"}
      </h1>

      {error && <p className="text-red-600">{error}</p>}

      {!error && products.length === 0 && (
        <p className="text-gray-600">No parts found for this vehicle yet.</p>
      )}

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
        {products.map((p, i) => (
          <Link
            key={p.id ?? i}
            href={`/shop/${p.slug}?model_id=${encodeURIComponent(model_id ?? "")}${year ? `&year=${encodeURIComponent(year)}` : ""}`}
            className="rounded border border-gray-200 p-3 hover:shadow"
          >
            {p.image_url && (
              <ProductImage
                src={p.image_url}
                fallback={p.image_fallback}
                className="mb-2 aspect-square w-full object-cover"
              />
            )}
            <p className="text-sm font-medium">{p.title ?? p.name}</p>
            {p.price != null && <p className="text-sm text-gray-700">${p.price}</p>}
          </Link>
        ))}
      </div>
    </>
  );
}

export default function Shop({ searchParams }: { searchParams: Params }) {
  return (
    <main className="mx-auto max-w-5xl p-6">
      <Link href="/" className="text-sm text-gray-600 underline">
        &larr; Change vehicle
      </Link>
      <Suspense fallback={<p className="mt-4 text-gray-600">Loading parts...</p>}>
        <Results searchParams={searchParams} />
      </Suspense>
    </main>
  );
}
