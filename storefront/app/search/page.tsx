import { Suspense } from "react";
import Link from "next/link";
import ProductImage from "../../components/site/ProductImage";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";
const PAGE_SIZE = 24;

type Item = {
  id: number;
  title: string;
  slug: string;
  price: string | null;
  universal: boolean;
  image_url: string | null;
  image_fallback: string | null;
};
type Params = Promise<{
  q?: string;
  category?: string;
  year?: string;
  model_id?: string;
  page?: string;
}>;
type Cat = { name: string; slug: string };

function pageHref(p: Awaited<Params>, page: number) {
  const qs = new URLSearchParams();
  if (p.q) qs.set("q", p.q);
  if (p.category) qs.set("category", p.category);
  if (p.year && p.model_id) {
    qs.set("year", p.year);
    qs.set("model_id", p.model_id);
  }
  if (page > 1) qs.set("page", String(page));
  return `/search?${qs}`;
}

async function Results({ searchParams }: { searchParams: Params }) {
  const p = await searchParams;
  const page = Math.max(1, Number(p.page) || 1);

  const qs = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String((page - 1) * PAGE_SIZE) });
  if (p.q) qs.set("q", p.q);
  if (p.category) qs.set("category", p.category);
  if (p.year && p.model_id) {
    qs.set("year", p.year);
    qs.set("model_id", p.model_id);
  }

  let items: Item[] = [];
  let total = 0;
  let error = "";
  let categoryName = "";
  try {
    const res = await fetch(`${API}/store/search?${qs}`, { cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    items = data.items ?? [];
    total = data.total ?? 0;
    if (p.category) {
      const cres = await fetch(`${API}/store/categories`, { cache: "no-store" });
      if (cres.ok) {
        const cats: Cat[] = await cres.json();
        categoryName = cats.find((c) => c.slug === p.category)?.name ?? "";
      }
    }
  } catch {
    error = "Could not load products right now. Please try again.";
  }

  const heading = p.q ? `Results for “${p.q}”` : categoryName || "All parts";
  const filtered = !!(p.year && p.model_id);
  const clearHref = pageHref({ ...p, year: undefined, model_id: undefined }, 1);
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <>
      <h1 className="mb-1 mt-2 text-2xl font-bold">{heading}</h1>
      <p className="mb-4 text-sm text-gray-600">
        {total} product{total === 1 ? "" : "s"}
        {filtered && (
          <>
            {" "}fitting your selected {p.year} machine.{" "}
            <Link href={clearHref} className="underline">Show all</Link>
          </>
        )}
      </p>

      {error && <p className="text-red-600">{error}</p>}
      {!error && items.length === 0 && <p className="text-gray-600">No products found.</p>}

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
        {items.map((it) => (
          <Link
            key={it.id}
            href={`/shop/${it.slug}${filtered ? `?year=${encodeURIComponent(p.year ?? "")}&model_id=${encodeURIComponent(p.model_id ?? "")}` : ""}`}
            className="rounded border border-gray-200 p-3 hover:shadow"
          >
            {it.image_url && (
              <ProductImage
                src={it.image_url}
                fallback={it.image_fallback}
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
          {page > 1 && <Link href={pageHref(p, page - 1)} className="underline">&larr; Previous</Link>}
          <span className="text-gray-600">Page {page} of {pages}</span>
          {page < pages && <Link href={pageHref(p, page + 1)} className="underline">Next &rarr;</Link>}
        </div>
      )}
    </>
  );
}

export default function Search({ searchParams }: { searchParams: Params }) {
  return (
    <main className="mx-auto max-w-6xl p-6">
      <Suspense fallback={<p className="text-gray-600">Loading…</p>}>
        <Results searchParams={searchParams} />
      </Suspense>
    </main>
  );
}
