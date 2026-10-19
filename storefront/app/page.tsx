import { Suspense } from "react";
import Link from "next/link";
import ProductImage from "../components/site/ProductImage";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";
const PAGE_SIZE = 24;

const TYPES = [
  { slug: "atv", name: "ATV" },
  { slug: "utv", name: "UTV" },
  { slug: "snowmobile", name: "Snowmobile" },
  { slug: "dirt-bike", name: "Dirt Bike" },
  { slug: "motorcycle", name: "Motorcycle" },
  { slug: "pwc", name: "PWC" },
];

type Item = {
  id: number;
  title: string;
  slug: string;
  price: string | null;
  universal: boolean;
  image_url: string | null;
  image_fallback: string | null;
};
type Params = Promise<{ page?: string; type?: string }>;

function pageHref(type: string | undefined, page: number) {
  const qs = new URLSearchParams();
  if (type) qs.set("type", type);
  if (page > 1) qs.set("page", String(page));
  const s = qs.toString();
  return s ? `/?${s}` : "/";
}

// Every published product, whatever machine it fits. Choosing a machine in the
// bar above ("Find parts") is what narrows the list to parts that fit.
async function AllProducts({ searchParams }: { searchParams: Params }) {
  const p = await searchParams;
  const page = Math.max(1, Number(p.page) || 1);

  let items: Item[] = [];
  let total = 0;
  let error = "";
  try {
    const qs = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String((page - 1) * PAGE_SIZE) });
    const res = await fetch(`${API}/store/search?${qs}`, { cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    items = data.items ?? [];
    total = data.total ?? 0;
  } catch {
    error = "Could not load products right now. Please try again.";
  }
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <>
      <p className="mb-4 text-sm text-gray-600">
        {total} product{total === 1 ? "" : "s"}
      </p>

      {error && <p className="text-red-600">{error}</p>}
      {!error && items.length === 0 && <p className="text-gray-600">No products found.</p>}

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
        {items.map((it) => (
          <Link key={it.id} href={`/shop/${it.slug}`} className="rounded border border-gray-200 p-3 hover:shadow">
            {it.image_url && (
              <ProductImage src={it.image_url} fallback={it.image_fallback} className="mb-2 aspect-square w-full object-cover" />
            )}
            <p className="text-sm font-medium">{it.title}</p>
            {it.price != null && <p className="text-sm text-gray-700">${it.price}</p>}
            {it.universal && <p className="mt-1 text-xs font-semibold text-[#0489ad]">Universal fit</p>}
          </Link>
        ))}
      </div>

      {pages > 1 && (
        <div className="mt-6 flex items-center gap-4 text-sm">
          {page > 1 && <Link href={pageHref(p.type, page - 1)} className="underline">&larr; Previous</Link>}
          <span className="text-gray-600">Page {page} of {pages}</span>
          {page < pages && <Link href={pageHref(p.type, page + 1)} className="underline">Next &rarr;</Link>}
        </div>
      )}
    </>
  );
}

export default function Home({ searchParams }: { searchParams: Params }) {
  return (
    <main className="mx-auto max-w-6xl px-4 py-10">
      <h1 className="text-3xl font-extrabold tracking-tight text-gray-900 sm:text-4xl">All aftermarket products</h1>
      <p className="mt-2 max-w-2xl text-gray-600">
        Browse everything below, or choose your machine in the bar above and we&apos;ll show only the parts that fit.
      </p>

      <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {TYPES.map((t) => (
          <Link
            key={t.slug}
            href={`/?type=${t.slug}`}
            className="rounded-lg border-2 border-gray-200 px-4 py-3 text-center font-bold text-gray-900 transition hover:border-[#06bde7] hover:shadow-md"
          >
            {t.name}
          </Link>
        ))}
      </div>

      <div className="mt-8">
        <Suspense fallback={<p className="text-gray-600">Loading products…</p>}>
          <AllProducts searchParams={searchParams} />
        </Suspense>
      </div>
    </main>
  );
}
