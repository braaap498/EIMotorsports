import { Suspense } from "react";
import Link from "next/link";
import { notFound } from "next/navigation";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

type Detail = {
  title: string;
  price: string | null;
  condition: string | null;
  part_number: string | null;
  sku: string;
  description: string | null;
  photos: string[];
  specifics: { name: string; value: string }[];
};

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
          // eslint-disable-next-line @next/next/no-img-element
          <img src={p.photos[0]} alt={p.title} className="w-full rounded border border-gray-200" />
        ) : (
          <div className="flex aspect-square items-center justify-center rounded bg-gray-100 text-gray-400">
            No photo
          </div>
        )}
        {p.photos.length > 1 && (
          <div className="mt-3 grid grid-cols-4 gap-2">
            {p.photos.slice(1, 9).map((src) => (
              // eslint-disable-next-line @next/next/no-img-element
              <img key={src} src={src} alt="" className="aspect-square w-full rounded border border-gray-200 object-cover" />
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
}: {
  params: Promise<{ slug: string }>;
}) {
  return (
    <main className="mx-auto max-w-5xl p-6">
      <Link href="/" className="text-sm text-gray-600 underline">
        &larr; Back to search
      </Link>
      <Suspense fallback={<p className="mt-4 text-gray-600">Loading...</p>}>
        <Product params={params} />
      </Suspense>
    </main>
  );
}
