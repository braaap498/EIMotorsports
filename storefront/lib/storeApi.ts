import { ApiError } from "./adminApi";
import { getSupabase } from "./supabase";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

/** Call a /store/... endpoint, sending the customer's token when signed in. */
export async function storeApi<T = unknown>(
  path: string,
  opts: { method?: string; body?: unknown } = {}
): Promise<T> {
  const { data } = await getSupabase().auth.getSession();
  const token = data.session?.access_token;
  const res = await fetch(`${API}/store${path}`, {
    method: opts.method ?? "GET",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  if (res.status === 204) return undefined as T;
  const json = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = typeof json?.detail === "string" ? json.detail : `Request failed (${res.status})`;
    throw new ApiError(res.status, detail);
  }
  return json as T;
}
