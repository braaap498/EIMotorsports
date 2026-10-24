import { getSupabase } from "./supabase";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : "Something went wrong";
}

// FastAPI sends `detail` as a string for our errors and as a list for
// validation errors; turn either into one readable sentence.
function detailText(detail: unknown, status: number): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) =>
        d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : ""
      )
      .filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  return `Request failed (${status})`;
}

/** Call an /admin/... endpoint on the API as the signed-in user. */
export async function api<T = unknown>(
  path: string,
  opts: { method?: string; body?: unknown } = {}
): Promise<T> {
  const { data } = await getSupabase().auth.getSession();
  const token = data.session?.access_token;
  if (!token) throw new ApiError(401, "Not signed in");

  const res = await fetch(`${API}/admin${path}`, {
    method: opts.method ?? "GET",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  if (res.status === 204) return undefined as T;
  const json = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, detailText(json?.detail, res.status));
  return json as T;
}

/** Download a file (Excel, PDF...) from an /admin/... endpoint as the signed-in user. */
export async function apiDownload(path: string): Promise<Blob> {
  const { data } = await getSupabase().auth.getSession();
  const token = data.session?.access_token;
  if (!token) throw new ApiError(401, "Not signed in");

  const res = await fetch(`${API}/admin${path}`, { headers: { Authorization: `Bearer ${token}` } });
  if (!res.ok) {
    const json = await res.json().catch(() => null);
    throw new ApiError(res.status, detailText(json?.detail, res.status));
  }
  return res.blob();
}
