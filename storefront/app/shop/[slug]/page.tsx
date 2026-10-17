import { Suspense } from "react";
import Link from "next/link";
import { notFound } from "next/navigation";
import ProductImage from "../../../components/site/ProductImage";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

type Detail = {
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

// Goes back to the parts list this product was opened from. If the page was
// opened directly (no year/model in the link), it falls back to the search.
async function BackLink({ searchParams }: { searchParams: Search }) {
  const { year, model_id } = await searchParams;
  const toResults = Boolean(year && model_id);
  const href = toResults
    ? `/shop?year=${encodeURIComponent(year!)}&model_id=${encodeURIComponent(model_id!)}`
    : "/";
  return (
    <Link href={href} className="text-sm text-gray-600 underline">
      &larr; {toResults ? "Back to parts" : "Back to search"}
    </Link>
  );
}

async function Product({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const res = await fetch(
    `${API}/store/fitment/product/${encodeURIComponent(slug)}`,
    { cache: "no-store" }
  );
  if (res.status === 404) notFound();
  if (!res.ok) {
    return <p className="text-red-600">Could not load this part right now.</p>;
  }
  const p: Detail = await res.json();

  return (
    <div className="mt-4 grid gap-8 md:grid-cols-2">
      <div>
        {p.photos[0] ? (
          <ProductImage
            src={p.photos[0].url}
            fallback={p.photos[0].fallback}
            alt={p.title}
            className="w-full rounded border border-gray-200"
          />
        ) : (
          <div className="flex aspect-square items-center justify-center rounded bg-gray-100 text-gray-400">
            No photo
          </div>
        )}
        {p.photos.length > 1 && (
          <div className="mt-3 grid grid-cols-4 gap-2">
            {p.photos.slice(1, 9).map((ph) => (
              <ProductImage
                key={ph.url}
                src={ph.thumb}
                fallback={ph.fallback}
                className="aspect-square w-full rounded border border-gray-200 object-cover"
              />
            ))}
          </div>
        )}
      </div>

      <div>
        <h1 className="text-2xl font-bold">{p.title}</h1>
        {p.price && <p className="mt-2 text-2xl">${p.price}</p>}
        <p className="mt-2 text-sm text-gray-500">
          {p.condition ? `${p.condition} · ` : ""}
          {p.part_number ? `Part # ${p.part_number} · ` : ""}SKU {p.sku}
        </p>

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
            className="mt-6 text-sm"
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
