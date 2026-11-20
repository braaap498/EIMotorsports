import type { Metadata } from "next";

// The public address of the site, used for canonical links and the sitemap.
// Set SITE_URL on Vercel to https://eandimotorsports.com when the domain goes live.
export const SITE_URL = (process.env.SITE_URL ?? "https://ei-motorsports.vercel.app").replace(/\/$/, "");

// Search engines are told to stay away until ALLOW_INDEXING=true is set on Vercel (then redeploy).
export const ALLOW_INDEXING = process.env.ALLOW_INDEXING === "true";

// The robots setting for a page. Before launch everything is noindex; after launch,
// pass index=false for pages that should be crawled but not listed (searches, filters).
export function robotsMeta(index = true): Metadata["robots"] {
  if (!ALLOW_INDEXING) return { index: false, follow: false };
  return { index, follow: true };
}

// HTML from the API -> plain text for meta descriptions, cut at a word boundary.
export function plainText(html: string | null | undefined, max = 155): string {
  const t = (html ?? "")
    .replace(/<[^>]*>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
  if (t.length <= max) return t;
  const cut = t.slice(0, max);
  return cut.slice(0, Math.max(cut.lastIndexOf(" "), max - 25)).trimEnd() + "…";
}

// JSON for a <script type="application/ld+json"> tag. Escapes "<" so text can never close the tag.
export function jsonLdString(data: unknown): string {
  return JSON.stringify(data).replace(/</g, "\\u003c");
}

// [2015, 2016, 2017, 2019] -> "2015–2017, 2019"
export function yearRanges(years: number[]): string {
  const ys = [...new Set(years)].filter((y) => Number.isFinite(y)).sort((a, b) => a - b);
  const parts: string[] = [];
  for (let i = 0; i < ys.length; ) {
    let j = i;
    while (j + 1 < ys.length && ys[j + 1] === ys[j] + 1) j++;
    parts.push(j > i ? `${ys[i]}–${ys[j]}` : `${ys[i]}`);
    i = j + 1;
  }
  return parts.join(", ");
}
