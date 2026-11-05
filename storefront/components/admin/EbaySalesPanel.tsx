"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Notice, Pager, btn2Cls, inputCls } from "./ui";
import { SortTh, useSort } from "./SortHeader";

/* The "eBay sales" box on the Live listings screen: how often each store is checked for new sales, a
   "check now" button, the switch that keeps eBay quantities in step, and the list of recent sales. */

type StoreSettings = {
  store: string;
  display_name: string | null;
  authorized: boolean;
  live_pushes_enabled: boolean;
  sales_polling_enabled: boolean;
  sales_poll_minutes: number;
  sales_last_polled_at: string | null;
  sales_last_ok_at: string | null;
  sales_last_summary: string | null;
  sales_last_error: string | null;
  sales_qty_push_enabled: boolean;
  running: boolean;
  qty_pending: number;
  qty_failed: number;
  set_aside: number;
  unmatched: number;
};

type Line = {
  id: number;
  store: string;
  ebay_order_id: string;
  ebay_item_id: string;
  title: string | null;
  quantity: number;
  unit_price: string | null;
  state: string;
  product_id: number | null;
  sku: string | null;
  sale_date: string | null;
  cancelled: boolean;
  refund_total: string | null;
};

type SortKey = "date" | "store" | "order" | "item" | "title" | "qty" | "price" | "state";

const MINUTES = [1, 2, 5, 10, 15, 30, 60, 120, 360, 1440];
const LIMIT = 50;
const STATE_LABEL: Record<string, string> = {
  pending: "Not paid yet",
  reserved: "Set aside",
  picked: "Picked",
  cancelled: "Cancelled",
  unmatched: "Not linked to a product",
  history: "History",
};
const STATE_TONE: Record<string, "gray" | "green" | "amber" | "blue"> = {
  pending: "gray",
  reserved: "blue",
  picked: "green",
  cancelled: "gray",
  unmatched: "amber",
  history: "gray",
};

function ago(iso: string | null) {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

export default function EbaySalesPanel() {
  const [stores, setStores] = useState<StoreSettings[]>([]);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  const [lines, setLines] = useState<Line[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [fStore, setFStore] = useState("");
  const [fState, setFState] = useState("");
  const [q, setQ] = useState("");
  const [qInput, setQInput] = useState("");
  const { sort, toggle } = useSort<SortKey>();

  const loadSettings = useCallback(async () => {
    try {
      const r = await api<{ stores: StoreSettings[] }>("/ebay-sales/settings");
      setStores(r.stores);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }, []);

  const loadLines = useCallback(async () => {
    try {
      const p = new URLSearchParams({ offset: String(offset), limit: String(LIMIT) });
      if (fStore) p.set("store", fStore);
      if (fState) p.set("state", fState);
      if (q) p.set("q", q);
      if (sort.key) {
        p.set("sort", sort.key);
        p.set("dir", sort.dir);
      }
      const r = await api<{ total: number; items: Line[] }>(`/ebay-sales/lines?${p.toString()}`);
      setLines(r.items);
      setTotal(r.total);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }, [offset, fStore, fState, q, sort.key, sort.dir]);

  useEffect(() => {
    void loadSettings();
    const t = setInterval(() => void loadSettings(), 30000);
    return () => clearInterval(t);
  }, [loadSettings]);

  useEffect(() => {
    void loadLines();
  }, [loadLines]);

  async function save(store: string, body: Record<string, unknown>) {
    setBusy(store);
    setMsg(null);
    try {
      await api("/ebay-sales/settings", { method: "PUT", body: { store, ...body } });
      await loadSettings();
    } catch (e) {
      setMsg({ kind: "error", text: `${store}: ${errMsg(e)}` });
    } finally {
      setBusy("");
    }
  }

  async function pollNow(store: string, days?: number) {
    if (days && !window.confirm(`Import the last ${days} days of orders for ${store}? Orders eBay already shows as shipped are recorded for the numbers only; they do not hold stock.`)) return;
    setBusy(store);
    setMsg(null);
    try {
      const r = await api<{ summary: string }>("/ebay-sales/poll-now", { method: "POST", body: { store, days } });
      setMsg({ kind: "success", text: `${store}: ${r.summary}` });
      await Promise.all([loadSettings(), loadLines()]);
    } catch (e) {
      setMsg({ kind: "error", text: `${store}: ${errMsg(e)}` });
      await loadSettings();
    } finally {
      setBusy("");
    }
  }

  async function feedAnalytics(store: string) {
    if (!window.confirm(`Send every saved paid order of ${store} to the eBay Analytics tab again? Rows that came from an uploaded report are never changed.`)) return;
    setBusy(store);
    setMsg(null);
    try {
      const r = await api<{ sent: number }>("/ebay-sales/feed-analytics", { method: "POST", body: { store } });
      setMsg({ kind: "success", text: `${store}: ${r.sent} orders sent to analytics.` });
    } catch (e) {
      setMsg({ kind: "error", text: `${store}: ${errMsg(e)}` });
    } finally {
      setBusy("");
    }
  }

  async function runQueue(store: string) {
    setBusy(store);
    setMsg(null);
    try {
      const r = await api<{ done: number; skipped: number; failed: number }>("/ebay-sales/queue/run", { method: "POST", body: { store } });
      setMsg({ kind: "success", text: `${store}: ${r.done} listings updated on eBay, ${r.skipped} skipped, ${r.failed} failed.` });
      await loadSettings();
    } catch (e) {
      setMsg({ kind: "error", text: `${store}: ${errMsg(e)}` });
    } finally {
      setBusy("");
    }
  }

  async function lineAction(id: number, what: "pick" | "release") {
    const q =
      what === "pick"
        ? "Take these parts off the shelf now? Use this only when the part has already gone and was not scanned."
        : "Stop setting these parts aside WITHOUT changing the shelf count?";
    if (!window.confirm(q)) return;
    setMsg(null);
    try {
      await api(`/ebay-sales/lines/${id}/${what}`, { method: "POST" });
      await Promise.all([loadLines(), loadSettings()]);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }

  const onSort = (k: SortKey) => {
    toggle(k);
    setOffset(0);
  };

  return (
    <div className="rounded border border-gray-200 bg-white p-4">
      <h2 className="text-sm font-semibold">eBay sales</h2>
      <p className="mt-1 text-xs text-gray-500">Check each store for new sales, set parts aside, and keep eBay quantities in step.</p>

      {(
        <div className="mt-3 space-y-4">
          {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

          <p className="text-xs text-gray-500">
            A sale sets its parts aside at once, so <strong>available</strong> drops. The shelf count (<strong>on hand</strong>) drops when
            the part is scanned out on the Ship screen with the eBay order number. While the server is asleep nothing is checked; when it wakes, the
            next check picks up from where the last good one ended, so no sale is missed.
          </p>

          <ul className="divide-y divide-gray-100 text-sm">
            {stores.map((s) => (
              <li key={s.store} className="space-y-2 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{s.display_name || s.store}</span>
                  <span className="text-gray-500">{s.store}</span>
                  {!s.authorized && <Badge tone="gray">Not connected</Badge>}
                  {s.running && <Badge tone="blue">Checking now…</Badge>}
                  {s.set_aside > 0 && <Badge tone="blue">{s.set_aside} sales set aside</Badge>}
                  {s.unmatched > 0 && <Badge tone="amber">{s.unmatched} not linked to a product</Badge>}
                </div>

                <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={s.sales_polling_enabled}
                      disabled={!s.authorized || busy === s.store}
                      onChange={(e) => void save(s.store, { sales_polling_enabled: e.target.checked })}
                    />
                    <span>Check for sales every</span>
                  </label>
                  <select
                    className={`${inputCls} !w-auto`}
                    value={s.sales_poll_minutes}
                    disabled={busy === s.store}
                    onChange={(e) => void save(s.store, { sales_poll_minutes: Number(e.target.value) })}
                  >
                    {MINUTES.includes(s.sales_poll_minutes) ? null : <option value={s.sales_poll_minutes}>{s.sales_poll_minutes} minutes</option>}
                    {MINUTES.map((m) => (
                      <option key={m} value={m}>
                        {m < 60 ? `${m} minute${m === 1 ? "" : "s"}` : m === 60 ? "1 hour" : m === 1440 ? "24 hours" : `${m / 60} hours`}
                      </option>
                    ))}
                  </select>
                  <label className={`flex items-center gap-2 ${s.live_pushes_enabled ? "" : "text-gray-400"}`}>
                    <input
                      type="checkbox"
                      checked={s.sales_qty_push_enabled && s.live_pushes_enabled}
                      disabled={!s.authorized || !s.live_pushes_enabled || busy === s.store}
                      onChange={(e) => void save(s.store, { sales_qty_push_enabled: e.target.checked })}
                    />
                    <span>Update quantities on eBay after a sale</span>
                  </label>
                  {!s.live_pushes_enabled && (
                    <span className="text-xs font-medium text-amber-700">
                      ⚠ Store pushes must be turned ON first (use “Turn pushes ON” in the Stores box above).
                    </span>
                  )}
                  <span className="ml-auto flex flex-wrap gap-2">
                    <button type="button" className={btn2Cls} disabled={!s.authorized || busy === s.store || s.running} onClick={() => void pollNow(s.store)}>
                      Check now
                    </button>
                    <button type="button" className={btn2Cls} disabled={!s.authorized || busy === s.store || s.running} onClick={() => void pollNow(s.store, 89)}>
                      Import 90 days
                    </button>
                    <button type="button" className={btn2Cls} disabled={!s.authorized || busy === s.store || s.running} onClick={() => void feedAnalytics(s.store)}>
                      Resend to analytics
                    </button>
                    {(s.qty_pending > 0 || s.qty_failed > 0) && (
                      <button type="button" className={btn2Cls} disabled={busy === s.store} onClick={() => void runQueue(s.store)}>
                        Send {s.qty_pending} quantity updates{s.qty_failed ? ` (${s.qty_failed} failed)` : ""}
                      </button>
                    )}
                  </span>
                </div>

                <div className="text-xs text-gray-500">
                  Last checked {ago(s.sales_last_polled_at)}
                  {s.sales_last_ok_at ? ` · last good check ${ago(s.sales_last_ok_at)}` : ""}
                  {s.sales_last_summary ? ` · ${s.sales_last_summary}` : ""}
                </div>
                {s.sales_last_error && <div className="text-xs text-red-700">Last problem: {s.sales_last_error}</div>}
              </li>
            ))}
            {stores.length === 0 && <li className="py-2 text-gray-500">No stores yet.</li>}
          </ul>

          <div className="flex flex-wrap items-end gap-2">
            <select className={`${inputCls} !w-auto`} value={fStore} onChange={(e) => { setFStore(e.target.value); setOffset(0); }}>
              <option value="">All stores</option>
              {stores.map((s) => (
                <option key={s.store} value={s.store}>
                  {s.display_name || s.store}
                </option>
              ))}
            </select>
            <select className={`${inputCls} !w-auto`} value={fState} onChange={(e) => { setFState(e.target.value); setOffset(0); }}>
              <option value="">Any state</option>
              {Object.entries(STATE_LABEL).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
            <input
              className={`${inputCls} !w-64`}
              placeholder="Order #, item # or title"
              value={qInput}
              onChange={(e) => setQInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  setQ(qInput.trim());
                  setOffset(0);
                }
              }}
            />
            <button type="button" className={btn2Cls} onClick={() => { setQ(qInput.trim()); setOffset(0); }}>
              Search
            </button>
          </div>

          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="bg-gray-50 text-xs text-gray-500">
                <tr>
                  <SortTh label="Date" k="date" sort={sort} onSort={onSort} />
                  <SortTh label="Store" k="store" sort={sort} onSort={onSort} />
                  <SortTh label="Order #" k="order" sort={sort} onSort={onSort} />
                  <SortTh label="Listing" k="item" sort={sort} onSort={onSort} />
                  <SortTh label="Title" k="title" sort={sort} onSort={onSort} />
                  <SortTh label="Qty" k="qty" sort={sort} onSort={onSort} />
                  <SortTh label="Price" k="price" sort={sort} onSort={onSort} />
                  <SortTh label="State" k="state" sort={sort} onSort={onSort} />
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {lines.map((l) => (
                  <tr key={l.id}>
                    <td className="whitespace-nowrap px-3 py-2 text-gray-600">{l.sale_date ? new Date(l.sale_date).toLocaleString() : "—"}</td>
                    <td className="px-3 py-2 text-gray-600">{l.store}</td>
                    <td className="whitespace-nowrap px-3 py-2">{l.ebay_order_id}</td>
                    <td className="whitespace-nowrap px-3 py-2">{l.ebay_item_id}</td>
                    <td className="max-w-xs px-3 py-2">
                      <div className="truncate" title={l.title ?? ""}>
                        {l.product_id ? (
                          <a className="hover:underline" href={`/admin/products/${l.product_id}`} target="_blank" rel="noopener noreferrer">
                            {l.title ?? "(no title)"}
                          </a>
                        ) : (
                          l.title ?? "(no title)"
                        )}
                      </div>
                      {l.sku && <div className="text-xs text-gray-500">{l.sku}</div>}
                    </td>
                    <td className="px-3 py-2">{l.quantity}</td>
                    <td className="px-3 py-2">{l.unit_price ? `$${Number(l.unit_price).toFixed(2)}` : "—"}</td>
                    <td className="px-3 py-2">
                      <Badge tone={STATE_TONE[l.state] ?? "gray"}>{STATE_LABEL[l.state] ?? l.state}</Badge>
                      {Number(l.refund_total) > 0 && (
                        <div className="mt-1 text-xs font-medium text-red-700">Refunded ${Number(l.refund_total).toFixed(2)}</div>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right">
                      {l.state === "reserved" && (
                        <span className="flex justify-end gap-2">
                          <button type="button" className={btn2Cls} onClick={() => void lineAction(l.id, "pick")}>
                            Mark picked
                          </button>
                          <button type="button" className={btn2Cls} onClick={() => void lineAction(l.id, "release")}>
                            Release
                          </button>
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
                {lines.length === 0 && (
                  <tr>
                    <td colSpan={9} className="px-3 py-6 text-center text-gray-500">
                      No eBay sales recorded yet. Turn on checking for a store, or press Check now.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <Pager total={total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </div>
      )}
    </div>
  );
}
