import { Suspense } from "react";
import Link from "next/link";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

type Product = {
  id?: string | number;
  title?: string;
  name?: string;
  price?: number | string;
  image_url?: string;
  slug?: string;
};

type Params = Promise<{ year?: string; model_id?: string }>;

async function Results({ searchParams }: { searchParams: Params }) {
  const { year, model_id } = await searchParams;

  let products: Product[] = [];
  let error = "";

  if (!year || !model_id) {
    error = "Pick a year, make, and model first.";
  } else {
    try {
      const res = await fetch(
        `${API}/store/fitment/products?year=${year}&model_id=${model_id}`,
        { cache: "no-store" }
      );
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      products = Array.isArray(data) ? data : data.items ?? data.products ?? [];
    } catch {
      error = "Could not load parts right now. Please try again.";
    }
  }

  return (
    <>
      <h1 className="mb-4 mt-2 text-2xl font-bold">
        Parts that fit your {year ?? ""} vehicle
      </h1>

      {error && <p className="text-red-600">{error}</p>}

      {!error && products.length === 0 && (
        <p className="text-gray-600">No parts found for this vehicle yet.</p>
      )}

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
        {products.map((p, i) => (
          <Link
            key={p.id ?? i}
            href={`/shop/${p.slug}?year=${encodeURIComponent(year ?? "")}&model_id=${encodeURIComponent(model_id ?? "")}`}
            className="rounded border border-gray-200 p-3 hover:shadow"
          >
            {p.image_url && (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={p.image_url} alt="" className="mb-2 aspect-square w-full object-cover" />
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
