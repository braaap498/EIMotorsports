"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, Pager, btn2Cls, inputCls } from "../ui";
import { SortTh, useSort } from "../SortHeader";
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

/** Who received or shipped what, who changed warehouse stock by hand, and order activity. */
export default function HistoryPanel({ active }: { active: boolean }) {
  const [view, setView] = useState<"orders" | "stock" | "warehouse">("orders");
  return (
    <div className="space-y-4">
      <div className="inline-flex overflow-hidden rounded border border-gray-300 text-sm">
        <button className={`px-4 py-2 ${view === "orders" ? "bg-black text-white" : "bg-white text-gray-700"}`} onClick={() => setView("orders")}>
          Order activity
        </button>
        <button className={`px-4 py-2 ${view === "stock" ? "bg-black text-white" : "bg-white text-gray-700"}`} onClick={() => setView("stock")}>
          Receiving and shipping
        </button>
        <button className={`px-4 py-2 ${view === "warehouse" ? "bg-black text-white" : "bg-white text-gray-700"}`} onClick={() => setView("warehouse")}>
          Warehouse changes
        </button>
      </div>
      <div className={view === "orders" ? "" : "hidden"}>
        <OrderActivityLog active={active && view === "orders"} />
      </div>
      <div className={view === "stock" ? "" : "hidden"}>
        <StockHistory active={active && view === "stock"} />
      </div>
      <div className={view === "warehouse" ? "" : "hidden"}>
        <WarehouseChanges active={active && view === "warehouse"} />
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

type Change = {
  id: number;
  at: string;
  user_email: string | null;
  warehouse_id: number | null;
  warehouse_name: string | null;
  product_id: number | null;
  sku: string | null;
  title: string | null;
  action: "added" | "count" | "removed" | "restock";
  old_value: string | null;
  new_value: string | null;
};
const CHANGE_LIMIT = 50;
type ChangeSort = "when" | "who" | "warehouse" | "part" | "change";

function describe(c: Change): string {
  const none = (v: string | null) => (v === null || v === "" ? "none" : v);
  switch (c.action) {
    case "added":
      return `Added to the warehouse with ${c.new_value}`;
    case "removed":
      return `Removed from the warehouse (had ${c.old_value})`;
    case "count": {
      const d = Number(c.new_value) - Number(c.old_value);
      return `Count ${c.old_value} → ${c.new_value} (${d > 0 ? "+" : ""}${d})`;
    }
    case "restock":
      return `Restock level ${none(c.old_value)} → ${none(c.new_value)}`;
  }
}

/** Manual stock changes in each warehouse: who made them, when, and where. Automatic picks are not listed here. */
function WarehouseChanges({ active }: { active: boolean }) {
  const [whs, setWhs] = useState<{ id: number; name: string }[]>([]);
  const [wh, setWh] = useState("");
  const [action, setAction] = useState("");
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [fFrom, setFFrom] = useState("");
  const [fTo, setFTo] = useState("");
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<Change[]>([]);
  const [total, setTotal] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const { sort, toggle } = useSort<ChangeSort>(); // nothing chosen = newest first

  useEffect(() => {
    if (!active) return;
    api<{ items: { id: number; name: string }[] }>("/inventory/warehouses")
      .then((r) => setWhs(r.items))
      .catch(() => {});
  }, [active]);

  // Wait for a pause in typing before searching.
  useEffect(() => {
    const t = setTimeout(() => {
      setQ(qInput.trim());
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [qInput]);

  const load = useCallback(() => {
    const p = new URLSearchParams({ limit: String(CHANGE_LIMIT), offset: String(offset) });
    if (wh) p.set("warehouse_id", wh);
    if (action) p.set("action", action);
    if (q) p.set("q", q);
    if (fFrom) p.set("date_from", fFrom);
    if (fTo) p.set("date_to", fTo);
    if (sort.key) {
      p.set("sort", sort.key);
      p.set("dir", sort.dir);
    }
    api<{ total: number; items: Change[] }>(`/inventory/warehouses/changes?${p.toString()}`)
      .then((r) => {
        setItems(r.items);
        setTotal(r.total);
        setError("");
        setLoaded(true);
      })
      .catch((e) => setError(errMsg(e)));
  }, [wh, action, q, fFrom, fTo, offset, sort.key, sort.dir]);
  useEffect(() => {
    if (active) load();
  }, [active, load]);

  const reset = () => setOffset(0);
  const onSort = (k: ChangeSort) => {
    toggle(k);
    setOffset(0);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <select className={`${inputCls} !w-56`} value={wh} onChange={(e) => { setWh(e.target.value); reset(); }}>
          <option value="">All warehouses</option>
          {whs.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </select>
        <select className={`${inputCls} !w-52`} value={action} onChange={(e) => { setAction(e.target.value); reset(); }}>
          <option value="">All changes</option>
          <option value="count">Count changes</option>
          <option value="added">Parts added</option>
          <option value="removed">Parts removed</option>
          <option value="restock">Restock level changes</option>
        </select>
        <input className={`${inputCls} !w-64`} placeholder="Search SKU, part or person" value={qInput} onChange={(e) => setQInput(e.target.value)} />
        <label className="text-xs text-gray-500">
          <span className="mb-1 block">From</span>
          <input type="date" className={`${inputCls} !w-auto`} value={fFrom} max={fTo || undefined} onChange={(e) => { setFFrom(e.target.value); reset(); }} />
        </label>
        <label className="text-xs text-gray-500">
          <span className="mb-1 block">To</span>
          <input type="date" className={`${inputCls} !w-auto`} value={fTo} min={fFrom || undefined} onChange={(e) => { setFTo(e.target.value); reset(); }} />
        </label>
        {(fFrom || fTo) && (
          <button type="button" className={btn2Cls} onClick={() => { setFFrom(""); setFTo(""); reset(); }}>
            Clear dates
          </button>
        )}
        <button className={btn2Cls} onClick={load}>
          Refresh
        </button>
      </div>
      {error && <Notice kind="error">{error}</Notice>}
      <Pager total={total} limit={CHANGE_LIMIT} offset={offset} onChange={setOffset} />
      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full min-w-[70rem] table-fixed text-left text-sm">
          <colgroup>
            <col className="w-[9rem]" />
            <col className="w-[16rem]" />
            <col className="w-[12rem]" />
            <col />
            <col className="w-[20rem]" />
          </colgroup>
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <SortTh label="When" k="when" sort={sort} onSort={onSort} />
              <SortTh label="Who" k="who" sort={sort} onSort={onSort} />
              <SortTh label="Warehouse" k="warehouse" sort={sort} onSort={onSort} />
              <SortTh label="Part" k="part" sort={sort} onSort={onSort} />
              <SortTh label="Change" k="change" sort={sort} onSort={onSort} />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {items.map((c) => (
              <tr key={c.id}>
                <td className="whitespace-nowrap px-3 py-2">
                  <div>{new Date(c.at).toLocaleDateString()}</div>
                  <div className="text-gray-500">{new Date(c.at).toLocaleTimeString()}</div>
                </td>
                <td className="truncate px-3 py-2" title={c.user_email ?? ""}>{c.user_email ?? "—"}</td>
                <td className="truncate px-3 py-2" title={c.warehouse_name ?? ""}>{c.warehouse_name ?? "—"}</td>
                <td className="px-3 py-2">
                  <div>{c.sku ?? "—"}</div>
                  <div className="truncate text-gray-500" title={c.title ?? ""}>{c.title}</div>
                </td>
                <td className="px-3 py-2">{describe(c)}</td>
              </tr>
            ))}
            {loaded && items.length === 0 && (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-gray-500">
                  Nothing recorded for these filters yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <Pager total={total} limit={CHANGE_LIMIT} offset={offset} onChange={setOffset} />
    </div>
  );
}
