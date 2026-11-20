import { cacheLife, cacheTag } from "next/cache";

// Server-side API reads that Next remembers, so a crawler (or a burst of shoppers) hitting
// thousands of pages does not send thousands of requests to the Render API.
//
//   "short": prices, stock, product lists, About text. Fresh within about 5 minutes.
//   "long":  categories, vehicle lists. Fresh within about an hour.
//
// Only successful answers are remembered. A failure (API down, 404) throws inside the cached
// function, so it is never stored, and the wrapper turns it into { status, data: null }.
// Do not use this for anything that depends on who is signed in (cart, account, checkout).
const API = process.env.NEXT_PUBLIC_API_URL ?? "";

async function cachedShort(path: string): Promise<unknown> {
  "use cache";
  cacheLife({ stale: 60, revalidate: 300, expire: 3600 });
  cacheTag("api", path);
  return load(path);
}

async function cachedLong(path: string): Promise<unknown> {
  "use cache";
  cacheLife({ stale: 300, revalidate: 3600, expire: 86400 });
  cacheTag("api", path);
  return load(path);
}

async function load(path: string): Promise<unknown> {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) throw new Error(`api-status:${res.status}`);
  return res.json();
}

async function live(path: string): Promise<unknown> {
  const res = await fetch(`${API}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`api-status:${res.status}`);
  return res.json();
}

export type ApiResult<T> = { status: number; data: T | null };

// status is 200 on success, the API's own status (404, 500...) on an error answer, 0 if unreachable.
export async function getJson<T>(
  path: string,
  opts: { ttl?: "short" | "long"; cache?: boolean } = {},
): Promise<ApiResult<T>> {
  const fn = opts.cache === false ? live : opts.ttl === "long" ? cachedLong : cachedShort;
  try {
    return { status: 200, data: (await fn(path)) as T };
  } catch (e) {
    const m = /api-status:(\d+)/.exec(e instanceof Error ? e.message : "");
    return { status: m ? Number(m[1]) : 0, data: null };
  }
}
