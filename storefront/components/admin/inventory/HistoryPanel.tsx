"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, btn2Cls, inputCls } from "../ui";

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
  const [kind, setKind] = useState("");
  const [items, setItems] = useState<Batch[]>([]);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api<{ items: Batch[] }>(`/inventory/history${kind ? `?kind=${kind}` : ""}`)
      .then((r) => {
        setItems(r.items);
        setError("");
      })
      .catch((e) => setError(errMsg(e)));
  }, [kind]);
  useEffect(() => {
    if (active) load();
  }, [active, load]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <select className={`${inputCls} w-48`} value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">Receiving and shipping</option>
          <option value="receive">Receiving only</option>
          <option value="ship">Shipping only</option>
        </select>
        <button className={btn2Cls} onClick={load}>
          Refresh
        </button>
      </div>
      {error && <Notice kind="error">{error}</Notice>}
      {items.length === 0 && !error && <p className="text-sm text-gray-500">Nothing recorded yet.</p>}
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
