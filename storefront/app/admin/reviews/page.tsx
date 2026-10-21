"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Field, Notice, btnCls, inputCls } from "../../../components/admin/ui";
import { useAccess } from "../../../components/admin/AdminAccess";

type Status = "pending" | "approved" | "hidden";
type SiteReview = {
  id: number;
  display_name: string;
  rating: number;
  title: string | null;
  body: string;
  status: Status;
  created_at: string;
};
type ReviewList = { items: SiteReview[]; counts: Record<Status, number> };
type EbayRow = { id: number; rating: "positive" | "neutral" | "negative"; comment: string; period: string };

const PERIODS = ["Past month", "Past 6 months", "Past year"];
const quiet =
  "rounded border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-800 hover:bg-gray-50 disabled:opacity-40";

function Stars({ value }: { value: number }) {
  return (
    <span className="leading-none" role="img" aria-label={`${value} out of 5 stars`}>
      <span className="text-amber-500">{"★".repeat(value)}</span>
      <span className="text-gray-300">{"★".repeat(5 - value)}</span>
    </span>
  );
}

function WebsiteReviews({ onCounts }: { onCounts: (c: Record<Status, number>) => void }) {
  const [status, setStatus] = useState<Status>("pending");
  const [data, setData] = useState<ReviewList | null>(null);
  const [removing, setRemoving] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api<ReviewList>(`/reviews?status=${status}`)
      .then((d) => {
        setData(d);
        onCounts(d.counts);
        setError("");
      })
      .catch((e: unknown) => setError(errMsg(e)));
  }, [status, onCounts]);

  useEffect(() => {
    setData(null);
    load();
  }, [load]);

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await fn();
      load();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  const label: Record<Status, string> = { pending: "Waiting", approved: "Showing", hidden: "Hidden" };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        {(["pending", "approved", "hidden"] as Status[]).map((s) => (
          <button
            key={s}
            type="button"
            aria-pressed={status === s}
            onClick={() => setStatus(s)}
            className={`rounded-full border px-3 py-1 text-sm font-medium ${
              status === s ? "border-gray-900 bg-gray-900 text-white" : "border-gray-300 bg-white text-gray-800 hover:bg-gray-50"
            }`}
          >
            {label[s]}
            {data ? ` (${data.counts[s]})` : ""}
          </button>
        ))}
      </div>

      {error && <Notice kind="error">{error}</Notice>}
      {!data && !error && <p className="text-sm text-gray-500">Loading…</p>}
      {data && data.items.length === 0 && (
        <p className="text-sm text-gray-500">
          {status === "pending" ? "Nothing waiting for approval." : status === "approved" ? "No reviews are showing on the site yet." : "No hidden reviews."}
        </p>
      )}

      <ul className="space-y-3">
        {data?.items.map((r) => (
          <li key={r.id} className="rounded-lg border border-gray-200 bg-white p-4">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <Stars value={r.rating} />
              {r.title && <span className="font-semibold">{r.title}</span>}
            </div>
            <p className="mt-1 whitespace-pre-line break-words text-sm text-gray-900">{r.body}</p>
            <p className="mt-1 text-xs text-gray-500">
              {r.display_name} · {new Date(r.created_at).toLocaleString("en-US")}
            </p>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {r.status !== "approved" && (
                <button type="button" className={btnCls} disabled={busy} onClick={() => void run(() => api(`/reviews/${r.id}/status`, { method: "POST", body: { status: "approved" } }))}>
                  Approve and show on site
                </button>
              )}
              {r.status === "approved" && (
                <button type="button" className={quiet} disabled={busy} onClick={() => void run(() => api(`/reviews/${r.id}/status`, { method: "POST", body: { status: "hidden" } }))}>
                  Hide from site
                </button>
              )}
              {r.status === "pending" && (
                <button type="button" className={quiet} disabled={busy} onClick={() => void run(() => api(`/reviews/${r.id}/status`, { method: "POST", body: { status: "hidden" } }))}>
                  Reject (hide)
                </button>
              )}
              {removing === r.id ? (
                <span className="flex items-center gap-2 text-sm">
                  <span className="text-gray-700">Delete for good?</span>
                  <button type="button" className="font-semibold text-red-700 underline" onClick={() => { setRemoving(null); void run(() => api(`/reviews/${r.id}`, { method: "DELETE" })); }}>
                    Yes, delete
                  </button>
                  <button type="button" className="underline" onClick={() => setRemoving(null)}>Keep</button>
                </span>
              ) : (
                <button type="button" className="text-sm text-red-700 underline" onClick={() => setRemoving(r.id)}>Delete</button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

type EbayAdmin = {
  items: EbayRow[];
  synced_count: number;
  status: {
    configured: boolean;
    seller: string;
    last_attempt_at: string | null;
    last_success_at: string | null;
    last_added: number | null;
    last_error: string | null;
  };
};

function EbayComments() {
  const [data, setData] = useState<EbayAdmin | null>(null);
  const [rating, setRating] = useState<EbayRow["rating"]>("positive");
  const [period, setPeriod] = useState(PERIODS[0]);
  const [comment, setComment] = useState("");
  const [removing, setRemoving] = useState<number | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    api<EbayAdmin>("/reviews/ebay")
      .then(setData)
      .catch((e: unknown) => setError(errMsg(e)));
  }, []);

  async function sync() {
    setSyncing(true);
    setError("");
    setNotice("");
    try {
      const d = await api<EbayAdmin>("/reviews/ebay/sync", { method: "POST" });
      setData(d);
      if (!d.status.last_error) {
        setNotice(
          d.status.last_added
            ? `Done. ${d.status.last_added.toLocaleString("en-US")} new comments were added.`
            : "Done. There was nothing new on eBay."
        );
      }
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setSyncing(false);
    }
  }

  async function add() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      setData(await api<EbayAdmin>("/reviews/ebay", { method: "POST", body: { rating, period, comment } }));
      setComment("");
      setNotice("Added. It is at the top of the eBay tab on the About page.");
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: number) {
    setRemoving(null);
    setError("");
    setNotice("");
    try {
      setData(await api<EbayAdmin>(`/reviews/ebay/${id}`, { method: "DELETE" }));
    } catch (e) {
      setError(errMsg(e));
    }
  }

  const st = data?.status;
  const when = (s: string | null) => (s ? new Date(s).toLocaleString("en-US") : "never");

  return (
    <div className="space-y-4">
      <div className="space-y-2 rounded-lg border border-gray-200 bg-white p-4 text-sm">
        <p className="font-medium text-gray-900">Comments from eBay</p>
        <p className="text-gray-700">
          {data ? `${data.synced_count.toLocaleString("en-US")} comments are saved from eBay.` : "Loading…"} They all show in the
          scrolling list on the About page. New ones are picked up about once a day when someone views that page.
        </p>
        {st && (
          <p className="text-xs text-gray-500">
            Last successful check: {when(st.last_success_at)}. Last try: {when(st.last_attempt_at)}.
          </p>
        )}
        {st && !st.configured && (
          <p className="text-amber-700">
            eBay keys are not set on the API yet (EBAY_CLIENT_ID and EBAY_CLIENT_SECRET), so it cannot read your comments.
          </p>
        )}
        {st?.last_error && (
          <p className="text-amber-700">
            The last try did not work: {st.last_error} Your saved comments are unchanged.
          </p>
        )}
        <button type="button" className={btnCls} disabled={syncing || !st?.configured} onClick={() => void sync()}>
          {syncing ? "Reading eBay… this can take a minute" : `Sync from eBay now (${st?.seller ?? "seller"})`}
        </button>
      </div>

      {notice && <Notice kind="success">{notice}</Notice>}
      {error && <Notice kind="error">{error}</Notice>}

      <details className="rounded-lg border border-gray-200 bg-white p-4">
        <summary className="cursor-pointer text-sm font-medium text-gray-900">Add a comment by hand</summary>
        <div className="mt-3 space-y-3">
          <p className="text-sm text-gray-700">
            Only needed if the sync can&apos;t reach eBay. Customers see these as comments from eBay, so only add ones that are
            really on your eBay feedback page, copied exactly as the buyer wrote them. Once the same comment arrives from eBay,
            the typed copy is removed automatically.
          </p>
          <Field label="Comment">
            <textarea className={`${inputCls} h-24`} value={comment} maxLength={1000} onChange={(e) => setComment(e.target.value)} />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Rating">
              <select className={inputCls} value={rating} onChange={(e) => setRating(e.target.value as EbayRow["rating"])}>
                <option value="positive">Positive</option>
                <option value="neutral">Neutral</option>
                <option value="negative">Negative</option>
              </select>
            </Field>
            <Field label="When (as eBay shows it)">
              <select className={inputCls} value={period} onChange={(e) => setPeriod(e.target.value)}>
                {PERIODS.map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </select>
            </Field>
          </div>
          <button type="button" className={btnCls} disabled={busy || !comment.trim()} onClick={() => void add()}>
            {busy ? "Adding…" : "Add comment"}
          </button>
        </div>
      </details>

      <div>
        <p className="mb-2 text-sm font-medium text-gray-700">Typed in by hand ({data?.items.length ?? 0})</p>
        <ul className="divide-y divide-gray-200 rounded-lg border border-gray-200 bg-white">
          {data?.items.map((r) => (
            <li key={r.id} className="flex flex-wrap items-start justify-between gap-3 px-4 py-3 text-sm">
              <div className="min-w-0 flex-1">
                <p className="whitespace-pre-line break-words text-gray-900">{r.comment}</p>
                <p className="mt-0.5 text-xs text-gray-500">
                  {r.rating[0].toUpperCase() + r.rating.slice(1)} · {r.period}
                </p>
              </div>
              {removing === r.id ? (
                <span className="flex items-center gap-2">
                  <span className="text-gray-700">Remove it?</span>
                  <button type="button" className="font-semibold text-red-700 underline" onClick={() => void remove(r.id)}>Yes, remove</button>
                  <button type="button" className="underline" onClick={() => setRemoving(null)}>Keep</button>
                </span>
              ) : (
                <button type="button" className="text-red-700 underline" onClick={() => setRemoving(r.id)}>Remove</button>
              )}
            </li>
          ))}
          {data && data.items.length === 0 && <li className="px-4 py-3 text-sm text-gray-500">None. Everything shown comes from eBay.</li>}
        </ul>
      </div>
    </div>
  );
}

export default function AdminReviewsPage() {
  const { isAdmin } = useAccess();
  const [tab, setTab] = useState<"site" | "ebay">("site");
  const [pending, setPending] = useState<number | null>(null);
  const onCounts = useCallback((c: Record<Status, number>) => setPending(c.pending), []);

  if (!isAdmin) return <p className="text-sm text-gray-600">Only admins can manage reviews.</p>;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Customer reviews</h1>
        <p className="mt-1 text-sm text-gray-600">
          What shows on the About page under &ldquo;See what customers say&rdquo;. New website reviews wait here until you approve them.
        </p>
      </div>

      <div className="flex gap-1 border-b border-gray-200" role="tablist">
        {(
          [
            ["site", "Website reviews", pending ? ` (${pending} waiting)` : ""],
            ["ebay", "eBay comments", ""],
          ] as const
        ).map(([key, label, extra]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            onClick={() => setTab(key)}
            className={`-mb-px rounded-t border px-4 py-2 text-sm font-medium ${
              tab === key ? "border-gray-200 border-b-white bg-white text-gray-900" : "border-transparent text-gray-500 hover:text-gray-800"
            }`}
          >
            {label}
            {extra}
          </button>
        ))}
      </div>

      <div hidden={tab !== "site"}>
        <WebsiteReviews onCounts={onCounts} />
      </div>
      <div hidden={tab !== "ebay"}>
        <EbayComments />
      </div>
    </div>
  );
}
