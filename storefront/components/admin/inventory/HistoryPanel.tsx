"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, btn2Cls, inputCls } from "../ui";
import OrderActivityLog from "./OrderActivityLog";

type Batch = {
  id: string;
  kind: "receive" | "ship";
  reference: string | null;
  note: string | null;
  created_by_email: string | null;
  created_at: string;
  order_number: string | null;
  lines: { product_id: number; sku: string | null; title: string | null; quantity: number }[];
};

/** Who received or shipped what, and when. */
export default function HistoryPanel({ active }: { active: boolean }) {
  const [view, setView] = useState<"orders" | "stock">("orders");
  return (
    <div className="space-y-4">
      <div className="inline-flex overflow-hidden rounded border border-gray-300 text-sm">
        <button className={`px-4 py-2 ${view === "orders" ? "bg-black text-white" : "bg-white text-gray-700"}`} onClick={() => setView("orders")}>
          Order activity
        </button>
        <button className={`px-4 py-2 ${view === "stock" ? "bg-black text-white" : "bg-white text-gray-700"}`} onClick={() => setView("stock")}>
          Receiving and shipping
        </button>
      </div>
      <div className={view === "orders" ? "" : "hidden"}>
        <OrderActivityLog active={active && view === "orders"} />
      </div>
      <div className={view === "stock" ? "" : "hidden"}>
        <StockHistory active={active && view === "stock"} />
      </div>
    </div>
  );
}

function StockHistory({ active }: { active: boolean }) {
  const [kind, setKind] = useState("");
  const [fFrom, setFFrom] = useState("");
  const [fTo, setFTo] = useState("");
  const [items, setItems] = useState<Batch[]>([]);
  const [error, setError] = useState("");

  // With no dates the newest 40 show; with dates the server sends up to 200 so a whole range is visible.
  const LIMIT = fFrom || fTo ? 200 : 40;
  const load = useCallback(() => {
    const p = new URLSearchParams({ limit: String(LIMIT) });
    if (kind) p.set("kind", kind);
    if (fFrom) p.set("date_from", fFrom);
    if (fTo) p.set("date_to", fTo);
    api<{ items: Batch[] }>(`/inventory/history?${p.toString()}`)
      .then((r) => {
        setItems(r.items);
        setError("");
      })
      .catch((e) => setError(errMsg(e)));
  }, [kind, fFrom, fTo, LIMIT]);
  useEffect(() => {
    if (active) load();
  }, [active, load]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <select className={`${inputCls} w-48`} value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">Receiving and shipping</option>
          <option value="receive">Receiving only</option>
          <option value="ship">Shipping only</option>
        </select>
        <label className="text-xs text-gray-500">
          <span className="mb-1 block">From</span>
          <input type="date" className={`${inputCls} !w-auto`} value={fFrom} max={fTo || undefined} onChange={(e) => { setFFrom(e.target.value); }} />
        </label>
        <label className="text-xs text-gray-500">
          <span className="mb-1 block">To</span>
          <input type="date" className={`${inputCls} !w-auto`} value={fTo} min={fFrom || undefined} onChange={(e) => { setFTo(e.target.value); }} />
        </label>
        {(fFrom || fTo) && (
          <button type="button" className={btn2Cls} onClick={() => { setFFrom(""); setFTo(""); }}>
            Clear dates
          </button>
        )}
        <button className={btn2Cls} onClick={load}>
          Refresh
        </button>
      </div>
      {error && <Notice kind="error">{error}</Notice>}
      {items.length === 0 && !error && <p className="text-sm text-gray-500">{fFrom || fTo ? "Nothing recorded in those dates." : "Nothing recorded yet."}</p>}
      {items.length >= LIMIT && <p className="text-xs text-gray-500">Showing the newest {LIMIT}. Narrow the dates to see older ones.</p>}
      <ul className="divide-y divide-gray-100 rounded-lg border border-gray-200 bg-white">
        {items.map((b) => {
          const units = b.lines.reduce((n, l) => n + l.quantity, 0);
          return (
            <li key={b.id}>
              <details className="px-4 py-3 text-sm">
                <summary className="flex cursor-pointer flex-wrap items-center gap-2">
                  <Badge tone={b.kind === "receive" ? "green" : "blue"}>{b.kind === "receive" ? "Received" : "Shipped"}</Badge>
                  <span className="font-medium">
                    {units} unit{units === 1 ? "" : "s"}
                  </span>
                  {(b.order_number || b.reference) && <span className="text-gray-600">· {b.order_number ?? b.reference}</span>}
                  <span className="text-gray-500">
                    · {new Date(b.created_at).toLocaleString()} · {b.created_by_email}
                  </span>
                </summary>
                <ul className="mt-2 space-y-1 pl-4 text-gray-700">
                  {b.lines.map((l, i) => (
                    <li key={i}>
                      {l.quantity} × {l.sku} — {l.title}
                    </li>
                  ))}
                  {b.note && <li className="italic text-gray-500">Note: {b.note}</li>}
                </ul>
              </details>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
