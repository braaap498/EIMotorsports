import { connection } from "next/server";
import { ALLOW_INDEXING, SITE_URL } from "../../lib/site";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";
const MAX_URLS = 50000; // the sitemap limit; past this the file needs to become an index of several

type PageList = { categories: string[]; vehicles: { make: string; model: string }[] };
type ProductList = { total: number; items: { slug: string; updated_at: string | null }[] };

const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} ${res.status}`);
  return (await res.json()) as T;
}

function xml(urls: { loc: string; lastmod?: string | null }[]) {
  const body = urls
    .map((u) => `<url><loc>${esc(u.loc)}</loc>${u.lastmod ? `<lastmod>${esc(u.lastmod)}</lastmod>` : ""}</url>`)
    .join("");
  return `<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${body}</urlset>`;
}

const HEADERS = {
  "Content-Type": "application/xml; charset=utf-8",
  // Cached at the edge for an hour, so crawlers do not each hit the API.
  "Cache-Control": "public, s-maxage=3600, stale-while-revalidate=86400",
};

export async function GET() {
  await connection(); // built on each request, not once at build time
  // Before launch the sitemap is empty (robots.txt also keeps crawlers out).
  if (!ALLOW_INDEXING) return new Response(xml([]), { headers: HEADERS });

  try {
    const pages = await getJson<PageList>("/store/seo/sitemap/pages");
    const urls: { loc: string; lastmod?: string | null }[] = [
      { loc: `${SITE_URL}/` },
      { loc: `${SITE_URL}/about` },
      { loc: `${SITE_URL}/parts` },
    ];
    for (const make of [...new Set(pages.vehicles.map((v) => v.make))]) urls.push({ loc: `${SITE_URL}/parts/${make}` });
    for (const v of pages.vehicles) urls.push({ loc: `${SITE_URL}/parts/${v.make}/${v.model}` });
    for (const c of pages.categories) urls.push({ loc: `${SITE_URL}/search?category=${encodeURIComponent(c)}` });

    const LIMIT = 5000;
    for (let offset = 0; urls.length < MAX_URLS; offset += LIMIT) {
      const batch = await getJson<ProductList>(`/store/seo/sitemap/products?limit=${LIMIT}&offset=${offset}`);
      for (const it of batch.items) urls.push({ loc: `${SITE_URL}/shop/${it.slug}`, lastmod: it.updated_at });
      if (batch.items.length < LIMIT) break;
    }
    return new Response(xml(urls.slice(0, MAX_URLS)), { headers: HEADERS });
  } catch {
    // Better an error crawlers retry than an empty or half-built list.
    return new Response("Sitemap temporarily unavailable", { status: 503, headers: { "Retry-After": "600" } });
  }
}
