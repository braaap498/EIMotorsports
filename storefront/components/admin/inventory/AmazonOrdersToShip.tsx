"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, Pager, btn2Cls, inputCls } from "../ui";

/* Inventory > Pick & ship > Amazon orders: every Amazon order line whose parts are still set aside, oldest first.
   Per line: Mark picked / Release. Per order: carrier + tracking, then Mark shipped (tells Amazon; needs Live pushes ON for the account).
   The Orders screen under Amazon shows the same orders read only. Uses the employee routes (inventory.ship), not the admin-only ones. */

type Line = {
  id: number; order_pk: number; account: string; amazon_order_id: string; sku: string | null; asin: string | null; title: string | null; quantity: number;
  pieces_per_sale: number; state: string; sale_date: string | null; cancelled: boolean; shipped_at: string | null;
  tracking_number: string | null; tracking_carrier: string | null; product_sku: string | null;
};

const BASE = "/inventory/amazon-orders";
const CARRIERS = ["USPS", "UPS", "FedEx", "DHL", "Other"] as const;
const LIMIT = 50;

export default function AmazonOrdersToShip({
  active,
  embedded = false,
  initialQ = "",
  reloadKey = 0,
  onEmpty,
}: {
  active: boolean;
  embedded?: boolean;
  initialQ?: string;
  reloadKey?: number;
  onEmpty?: () => void;
}) {
  const [lines, setLines] = useState<Line[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [q, setQ] = useState(initialQ);
  const [qInput, setQInput] = useState(initialQ);
  const [msg, setMsg] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [carrier, setCarrier] = useState<Record<number, string>>({});
  const [track, setTrack] = useState<Record<number, string>>({});
  const emptyRef = useRef(onEmpty);
  emptyRef.current = onEmpty;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const p = new URLSearchParams({ offset: String(offset), limit: String(LIMIT) });
      if (q) p.set("q", q);
      const r = await api<{ total: number; items: Line[] }>(`${BASE}/lines?${p}`);
      setLines(r.items);
      setTotal(r.total);
      if (r.items.length === 0) emptyRef.current?.();
    } catch (e) {
      setMsg(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, [offset, q]);

  useEffect(() => {
    if (active) void load();
  }, [active, load, reloadKey]);

  async function post(path: string, body?: unknown) {
    setMsg(null);
    try {
      await api(path, { method: "POST", body });
      await load();
    } catch (e) {
      setMsg(errMsg(e));
    }
  }

  return (
    <div className="space-y-3">
      {!embedded && (
        <p className="text-xs text-gray-500">
          Amazon orders whose parts are still set aside, oldest first. Buy the label on Amazon, then enter the carrier and tracking number and press <strong>Mark shipped</strong>.
        </p>
      )}
      {msg && <Notice kind="error">{msg}</Notice>}
      {!embedded && (
        <div className="flex flex-wrap items-end gap-2">
          <input className={`${inputCls} !w-[28rem] max-w-full`} placeholder="Order #, SKU, ASIN or title" value={qInput} onChange={(e) => setQInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { setQ(qInput.trim()); setOffset(0); } }} />
          <button type="button" className={btn2Cls} onClick={() => { setQ(qInput.trim()); setOffset(0); }}>Search</button>
          <button type="button" className={btn2Cls} disabled={loading} onClick={() => { setMsg(null); void load(); }}>{loading ? "Loading…" : "Refresh"}</button>
        </div>
      )}
      {!embedded && <Pager total={total} limit={LIMIT} offset={offset} onChange={setOffset} />}
      <div className="overflow-x-auto rounded border border-gray-200 bg-white">
        <table className="w-full min-w-[60rem] text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr><th className="px-3 py-2">Date</th><th className="px-3 py-2">Order #</th><th className="px-3 py-2">Item</th><th className="px-3 py-2">Qty</th><th className="px-3 py-2">Pick</th><th className="px-3 py-2">Shipping</th></tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {lines.map((l) => (
              <tr key={l.id} className={l.shipped_at ? "bg-green-50" : ""}>
                <td className="whitespace-nowrap px-3 py-2 text-gray-600">{l.sale_date ? new Date(l.sale_date).toLocaleString() : "—"}</td>
                <td className="whitespace-nowrap px-3 py-2">{l.amazon_order_id}</td>
                <td className="max-w-[22rem] px-3 py-2">
                  <div className="truncate" title={l.title ?? ""}>{l.title ?? "(no title)"}</div>
                  <div className="text-xs text-blue-600">{l.sku ?? "—"}{l.product_sku ? ` · ${l.product_sku}` : ""}</div>
                </td>
                <td className="px-3 py-2">
                  {l.quantity}
                  {l.pieces_per_sale > 1 && <div className="whitespace-nowrap text-xs font-medium text-amber-700">× {l.pieces_per_sale} = {l.quantity * l.pieces_per_sale} pieces to pick</div>}
                </td>
                <td className="px-3 py-2">
                  <div className="flex gap-1.5">
                    <button type="button" className={`${btn2Cls} !px-2 !py-1 !text-xs`} onClick={() => void post(`${BASE}/lines/${l.id}/pick`)}>Mark picked</button>
                    <button type="button" className={`${btn2Cls} !px-2 !py-1 !text-xs`} onClick={() => { if (window.confirm("Release this line? Its parts stop being set aside (the shelf count does not change).")) void post(`${BASE}/lines/${l.id}/release`); }}>Release</button>
                  </div>
                </td>
                <td className="px-3 py-2">
                  {l.shipped_at ? (
                    <div className="space-y-1"><Badge tone="green">Shipped, waiting to be picked</Badge><div className="text-xs">{l.tracking_carrier} {l.tracking_number}</div></div>
                  ) : (
                    <div className="flex flex-wrap items-center gap-1.5">
                      <select className={`${inputCls} !w-24 !py-1`} value={carrier[l.order_pk] ?? "USPS"} onChange={(e) => setCarrier({ ...carrier, [l.order_pk]: e.target.value })} aria-label="Carrier">
                        {CARRIERS.map((c) => <option key={c} value={c}>{c}</option>)}
                      </select>
                      <input className={`${inputCls} !w-44 !py-1`} placeholder="Tracking number" value={track[l.order_pk] ?? ""} onChange={(e) => setTrack({ ...track, [l.order_pk]: e.target.value })} />
                      <button type="button" className={`${btn2Cls} !px-2 !py-1 !text-xs`} disabled={(track[l.order_pk] ?? "").trim().length < 6} onClick={() => void post(`${BASE}/orders/${l.order_pk}/ship`, { carrier: carrier[l.order_pk] ?? "USPS", tracking_number: (track[l.order_pk] ?? "").trim() })}>Mark shipped</button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
            {lines.length === 0 && <tr><td colSpan={6} className="px-3 py-6 text-center text-gray-500">{loading ? "Loading…" : "No Amazon orders waiting. Everything set aside has been picked or released."}</td></tr>}
          </tbody>
        </table>
      </div>
      {!embedded && <Pager total={total} limit={LIMIT} offset={offset} onChange={setOffset} />}
    </div>
  );
}
