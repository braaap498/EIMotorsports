"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Notice, btn2Cls, inputCls } from "./ui";

/* eBay order check, one row per store: how often the store is checked for new orders, "Check now", importing the
   last 90 days, the switch that keeps eBay quantities in step, and resending orders to analytics.
   Shown on the Store API > Stores page. The list of the orders themselves is on the Orders tab. */

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

const MINUTES = [1, 2, 5, 10, 15, 30, 60, 120, 360, 1440];

function ago(iso: string | null) {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

export default function EbayOrderCheck() {
  const [stores, setStores] = useState<StoreSettings[]>([]);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  const loadSettings = useCallback(async () => {
    try {
      const r = await api<{ stores: StoreSettings[] }>("/ebay-sales/settings");
      setStores(r.stores);
    } catch (e) {
      setMsg({ kind: "error", text: errMsg(e) });
    }
  }, []);

  useEffect(() => {
    void loadSettings();
    const t = setInterval(() => void loadSettings(), 30000);
    return () => clearInterval(t);
  }, [loadSettings]);

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
      await loadSettings();
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

  return (
    <div className="rounded border border-gray-200 bg-white p-4">
      <h2 className="text-sm font-semibold">eBay order check</h2>
      <p className="mt-1 text-xs text-gray-500">
        Check each store for new orders, set parts aside, and keep eBay quantities in step. The orders themselves are on the Orders tab.
      </p>

      <div className="mt-3 space-y-4">
        {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

        <p className="text-xs text-gray-500">
          An order sets its parts aside at once, so <strong>available</strong> drops. While the server is asleep nothing is checked; when it wakes, the
          next check picks up from where the last good one ended, so no order is missed.
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
      </div>
    </div>
  );
}
