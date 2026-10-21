"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { storeApi } from "../../lib/storeApi";
import { useAuth } from "./AuthProvider";

type EbayItem = { id: number; rating: "positive" | "neutral" | "negative"; comment: string; period: string };
type Review = { id: number; display_name: string; rating: number; title: string | null; body: string; created_at: string };
type Paged<T> = { items: T[]; total: number; average?: number | null };

const PAGE = 20;
const msg = (e: unknown) => (e instanceof Error ? e.message : "Something went wrong");
const input =
  "mt-1 w-full rounded border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 outline-none focus:border-[#06bde7] focus:ring-2 focus:ring-[#06bde7]/30";
const primary = "rounded bg-[#06bde7] px-4 py-2 text-sm font-bold text-[#04202b] hover:brightness-110 disabled:opacity-50";
const secondary = "rounded border border-gray-300 px-4 py-2 text-sm font-semibold text-gray-900 hover:bg-gray-50 disabled:opacity-50";

// Loads a list a page at a time. Nothing is requested until `enabled` is true.
function usePaged<T>(path: string, enabled: boolean) {
  const [items, setItems] = useState<T[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [average, setAverage] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const busy = useRef(false);
  const offset = useRef(0);

  const load = useCallback(
    async (reset: boolean) => {
      if (busy.current) return;
      busy.current = true;
      setLoading(true);
      setError("");
      try {
        const from = reset ? 0 : offset.current;
        const page = await storeApi<Paged<T>>(`${path}?offset=${from}&limit=${PAGE}`);
        setItems((cur) => (reset ? page.items : [...cur, ...page.items]));
        offset.current = from + page.items.length;
        setTotal(page.total);
        setAverage(page.average ?? null);
      } catch (e) {
        setError(msg(e));
      } finally {
        busy.current = false;
        setLoading(false);
      }
    },
    [path]
  );

  useEffect(() => {
    if (enabled && total === null && !busy.current) void load(true);
  }, [enabled, total, load]);

  return {
    items,
    total,
    average,
    loading,
    error,
    hasMore: total !== null && items.length < total,
    more: () => void load(false),
    reload: () => void load(true),
  };
}
type List<T> = ReturnType<typeof usePaged<T>>;

function RatingIcon({ rating }: { rating: EbayItem["rating"] }) {
  const color = rating === "positive" ? "text-green-600" : rating === "negative" ? "text-red-600" : "text-gray-400";
  const label = rating === "positive" ? "Positive" : rating === "negative" ? "Negative" : "Neutral";
  return (
    <svg viewBox="0 0 24 24" className={`mt-0.5 h-5 w-5 shrink-0 ${color}`} role="img" aria-label={label}>
      <circle cx="12" cy="12" r="11" fill="currentColor" />
      {rating === "neutral" ? (
        <circle cx="12" cy="12" r="2.5" fill="#fff" />
      ) : (
        <path
          d={rating === "positive" ? "M12 6.5v11M6.5 12h11" : "M6.5 12h11"}
          stroke="#fff"
          strokeWidth="2.6"
          strokeLinecap="round"
        />
      )}
    </svg>
  );
}

function Stars({ value, className = "text-base" }: { value: number; className?: string }) {
  const full = Math.max(0, Math.min(5, Math.round(value)));
  return (
    <span className={`${className} leading-none tracking-wide`} role="img" aria-label={`${value} out of 5 stars`}>
      <span className="text-amber-500">{"★".repeat(full)}</span>
      <span className="text-gray-300">{"★".repeat(5 - full)}</span>
    </span>
  );
}

// A box with its own scrollbar. Reaching the bottom loads the next page; the button
// is there for keyboard users and as a fallback.
function ScrollList({
  label,
  hasMore,
  loading,
  onMore,
  children,
}: {
  label: string;
  hasMore: boolean;
  loading: boolean;
  onMore: () => void;
  children: React.ReactNode;
}) {
  return (
    <div
      role="region"
      aria-label={label}
      tabIndex={0}
      onScroll={(e) => {
        const el = e.currentTarget;
        if (hasMore && el.scrollHeight - el.scrollTop - el.clientHeight < 160) onMore();
      }}
      className="max-h-[26rem] overflow-y-auto rounded border border-gray-200 bg-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#06bde7]"
    >
      <ul className="divide-y divide-gray-200">{children}</ul>
      {hasMore && (
        <div className="border-t border-gray-200 p-3 text-center">
          <button type="button" className={secondary} onClick={onMore} disabled={loading}>
            {loading ? "Loading…" : "Show more"}
          </button>
        </div>
      )}
    </div>
  );
}

function LoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
      {message}{" "}
      <button type="button" className="font-semibold underline" onClick={onRetry}>
        Try again
      </button>
    </div>
  );
}

function EbayPanel({ list, feedbackUrl }: { list: List<EbayItem>; feedbackUrl: string | null }) {
  if (list.error && list.items.length === 0) return <LoadError message={list.error} onRetry={list.reload} />;
  if (list.total === null) return <p className="py-6 text-sm text-gray-600">Loading feedback…</p>;
  if (list.total === 0) return <p className="py-6 text-sm text-gray-600">Our eBay feedback will show up here soon.</p>;
  return (
    <div className="space-y-2">
      <ScrollList label="eBay feedback" hasMore={list.hasMore} loading={list.loading} onMore={list.more}>
        {list.items.map((it) => (
          <li key={it.id} className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 px-4 py-3 sm:grid-cols-[auto_1fr_7rem]">
            <RatingIcon rating={it.rating} />
            <p className="min-w-0 whitespace-pre-line break-words text-sm text-gray-900">{it.comment}</p>
            <span className="col-start-2 text-xs text-gray-500 sm:col-start-3 sm:text-right">{it.period}</span>
          </li>
        ))}
      </ScrollList>
      {list.error && <LoadError message={list.error} onRetry={list.more} />}
      <p className="text-xs text-gray-500">
        Showing {list.items.length} of {list.total} comments from buyers in our eBay store.
        {feedbackUrl && (
          <>
            {" "}
            <a href={feedbackUrl} target="_blank" rel="noopener noreferrer" className="underline">
              See our full feedback page on eBay
            </a>
            .
          </>
        )}
      </p>
    </div>
  );
}

function shortName(full: string) {
  const p = full.trim().split(/\s+/).filter(Boolean);
  if (p.length === 0) return "";
  if (p.length === 1) return p[0];
  return `${p[0]} ${p[p.length - 1][0].toUpperCase()}.`;
}

function ReviewForm({ onDone, onCancel }: { onDone: (status: string) => void; onCancel: () => void }) {
  const { user } = useAuth();
  const full = (user?.user_metadata as { full_name?: string } | undefined)?.full_name ?? "";
  const [name, setName] = useState(shortName(full));
  const [touched, setTouched] = useState(false);
  const [rating, setRating] = useState(0);
  const [hover, setHover] = useState(0);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [website, setWebsite] = useState(""); // honeypot
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // Fill in the name once for signed-in customers, unless they have started typing their own.
  useEffect(() => {
    if (!touched && full) setName(shortName(full));
  }, [full, touched]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (rating < 1) return setError("Pick a star rating first.");
    if (body.trim().length < 10) return setError("Tell us a bit more. A sentence or two is plenty.");
    setBusy(true);
    setError("");
    try {
      const res = await storeApi<{ status: string }>("/reviews", {
        method: "POST",
        body: { display_name: name.trim(), rating, title: title.trim() || null, body: body.trim(), website },
      });
      onDone(res.status);
    } catch (err) {
      setError(msg(err));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="relative space-y-3 rounded border border-gray-200 bg-gray-50 p-4">
      <fieldset>
        <legend className="text-sm font-medium text-gray-900">Your rating</legend>
        <div className="mt-1 flex gap-1" onMouseLeave={() => setHover(0)}>
          {[1, 2, 3, 4, 5].map((n) => (
            <label key={n} className="cursor-pointer" onMouseEnter={() => setHover(n)}>
              <input
                type="radio"
                name="rating"
                value={n}
                checked={rating === n}
                onChange={() => setRating(n)}
                className="peer sr-only"
              />
              <span
                aria-hidden="true"
                className={`block text-3xl leading-none peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-[#06bde7] ${
                  n <= (hover || rating) ? "text-amber-500" : "text-gray-300"
                }`}
              >
                ★
              </span>
              <span className="sr-only">
                {n} {n === 1 ? "star" : "stars"}
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <label className="block text-sm font-medium text-gray-900">
        Name to show
        <input
          className={input}
          value={name}
          maxLength={60}
          minLength={2}
          required
          autoComplete="name"
          onChange={(e) => {
            setTouched(true);
            setName(e.target.value);
          }}
        />
        <span className="mt-1 block text-xs font-normal text-gray-500">First name and last initial is plenty.</span>
      </label>

      <label className="block text-sm font-medium text-gray-900">
        Headline (optional)
        <input className={input} value={title} maxLength={100} onChange={(e) => setTitle(e.target.value)} />
      </label>

      <label className="block text-sm font-medium text-gray-900">
        Your review
        <textarea
          className={`${input} h-28`}
          value={body}
          maxLength={2000}
          required
          onChange={(e) => setBody(e.target.value)}
          placeholder="What did you order, and how did it go?"
        />
      </label>

      {/* Honeypot: hidden from people, tempting to bots. */}
      <div className="absolute -left-[9999px] h-0 w-0 overflow-hidden" aria-hidden="true">
        <label>
          Website
          <input tabIndex={-1} autoComplete="off" value={website} onChange={(e) => setWebsite(e.target.value)} />
        </label>
      </div>

      {error && <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">{error}</p>}
      <div className="flex gap-2">
        <button className={primary} disabled={busy}>
          {busy ? "Sending…" : "Send review"}
        </button>
        <button type="button" className={secondary} onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function SitePanel({ list }: { list: List<Review> }) {
  const [writing, setWriting] = useState(false);
  const [sent, setSent] = useState<string | null>(null);

  if (list.error && list.items.length === 0) return <LoadError message={list.error} onRetry={list.reload} />;
  if (list.total === null) return <p className="py-6 text-sm text-gray-600">Loading reviews…</p>;

  const thanks =
    sent && (
      <p className="rounded border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-900">
        {sent === "approved"
          ? "Thanks for the review. It’s live now."
          : "Thanks for the review. We read every one, and yours will appear here once we’ve approved it."}
      </p>
    );

  const form = writing && (
    <ReviewForm
      onCancel={() => setWriting(false)}
      onDone={(status) => {
        setWriting(false);
        setSent(status);
        if (status === "approved") list.reload();
      }}
    />
  );

  if (list.total === 0) {
    return (
      <div className="space-y-3">
        {thanks}
        {form}
        {!writing && !sent && (
          <div className="rounded border border-dashed border-gray-300 bg-white px-5 py-8 text-center">
            <p className="font-semibold text-gray-900">No reviews here yet, which means the holeshot is yours.</p>
            <p className="mx-auto mt-1 max-w-md text-sm text-gray-600">
              Be the first one into turn one and tell us how we did. We promise not to let it go to our heads.
            </p>
            <button type="button" className={`${primary} mt-4`} onClick={() => setWriting(true)}>
              Write the first review
            </button>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="flex items-center gap-2 text-sm text-gray-700">
          {list.average != null && (
            <>
              <Stars value={list.average} className="text-lg" />
              <span className="font-semibold text-gray-900">{list.average.toFixed(1)}</span>
            </>
          )}
          <span>
            from {list.total} {list.total === 1 ? "review" : "reviews"}
          </span>
        </p>
        {!writing && (
          <button type="button" className={secondary} onClick={() => setWriting(true)}>
            Write a review
          </button>
        )}
      </div>
      {thanks}
      {form}
      <ScrollList label="Website reviews" hasMore={list.hasMore} loading={list.loading} onMore={list.more}>
        {list.items.map((r) => (
          <li key={r.id} className="px-4 py-3">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <Stars value={r.rating} />
              {r.title && <span className="text-sm font-semibold text-gray-900">{r.title}</span>}
            </div>
            <p className="mt-1 whitespace-pre-line break-words text-sm text-gray-900">{r.body}</p>
            <p className="mt-1 text-xs text-gray-500">
              {r.display_name} on{" "}
              {new Date(r.created_at).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}
            </p>
          </li>
        ))}
      </ScrollList>
      {list.error && <LoadError message={list.error} onRetry={list.more} />}
    </div>
  );
}

export default function CustomerFeedback({
  id,
  open,
  feedbackUrl,
}: {
  id: string;
  open: boolean;
  feedbackUrl: string | null;
}) {
  const [tab, setTab] = useState<"ebay" | "site">("ebay");
  const ebay = usePaged<EbayItem>("/ebay-feedback", open);
  const site = usePaged<Review>("/reviews", open);

  const tabs = [
    { key: "ebay", label: "From eBay", count: ebay.total },
    { key: "site", label: "Website reviews", count: site.total },
  ] as const;

  return (
    // Slides open and closed. `inert` keeps hidden content out of the tab order.
    <div
      id={id}
      className={`grid transition-[grid-template-rows] duration-300 ease-out motion-reduce:transition-none ${
        open ? "grid-rows-[1fr]" : "grid-rows-[0fr]"
      }`}
    >
      <div className="min-h-0 overflow-hidden" inert={!open}>
        <div className="px-0.5 pb-0.5 pt-5">
          <div className="flex gap-1 border-b border-gray-200" role="tablist" aria-label="Customer feedback">
            {tabs.map((t) => (
              <button
                key={t.key}
                type="button"
                role="tab"
                id={`${id}-tab-${t.key}`}
                aria-selected={tab === t.key}
                aria-controls={`${id}-panel-${t.key}`}
                onClick={() => setTab(t.key)}
                className={`-mb-px border-b-2 px-4 py-2 text-sm font-semibold ${
                  tab === t.key
                    ? "border-[#06bde7] text-gray-900"
                    : "border-transparent text-gray-500 hover:text-gray-800"
                }`}
              >
                {t.label}
                {t.count !== null && <span className="ml-1 font-normal text-gray-500">({t.count})</span>}
              </button>
            ))}
          </div>

          <div className="pt-4">
            <div role="tabpanel" id={`${id}-panel-ebay`} aria-labelledby={`${id}-tab-ebay`} hidden={tab !== "ebay"}>
              <EbayPanel list={ebay} feedbackUrl={feedbackUrl} />
            </div>
            <div role="tabpanel" id={`${id}-panel-site`} aria-labelledby={`${id}-tab-site`} hidden={tab !== "site"}>
              <SitePanel list={site} />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
