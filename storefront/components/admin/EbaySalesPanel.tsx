"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Badge, Notice, Pager, btn2Cls, inputCls } from "./ui";
import { SortTh, useSort } from "./SortHeader";

/* The list of eBay orders (the Orders tab): filter by store, state or order/item number; mark a set-aside order
   picked or release it. Checking stores for new orders (how often, "Check now", import 90 days, quantity
   updates) is on the Store API > Stores page. */

type StoreOption = { store: string; display_name: string | null };

type Line = {
  id: number;
  store: string;
  ebay_order_id: string;
  ebay_item_id: string;
  custom_label: string | null;
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

export default function EbaySalesPanel() {
  const [stores, setStores] = useState<StoreOption[]>([]);
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
      const r = await api<{ stores: StoreOption[] }>("/ebay-sales/settings");
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
  }, [loadSettings]);

  useEffect(() => {
    void loadLines();
  }, [loadLines]);

  async function lineAction(id: number, what: "pick" | "release") {
    const q =
      what === "pick"
        ? "Take these parts off the shelf now? Use this only when the part has already gone and was not scanned."
        : "Stop setting these parts aside WITHOUT changing the shelf count?";
    if (!window.confirm(q)) return;
    setMsg(null);
    try {
      await api(`/ebay-sales/lines/${id}/${what}`, { method: "POST" });
      await loadLines();
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
      <h2 className="text-sm font-semibold">eBay orders</h2>
      <p className="mt-1 text-xs text-gray-500">
        Recent orders from every store. How often stores are checked, Check now and Import 90 days are under{" "}
        <Link className="underline" href="/admin/ebay-stores">Store API &rsaquo; Stores</Link>.
      </p>

      {(
        <div className="mt-3 space-y-4">
          {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

          <p className="text-xs text-gray-500">
            An order sets its parts aside at once, so <strong>available</strong> drops. The shelf count (<strong>on hand</strong>) drops when
            the part is scanned out on the Ship screen with the eBay order number.
          </p>

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
              placeholder="Order #, item #, custom label or title"
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
                  <SortTh label="Custom label" k="item" sort={sort} onSort={onSort} />
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
                    <td className="whitespace-nowrap px-3 py-2">{l.custom_label || "—"}</td>
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
                      <div className="text-xs text-blue-600">
                        {l.ebay_item_id}
                        {l.sku ? ` · ${l.sku}` : ""}
                      </div>
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
                      No eBay orders recorded yet. Turn on order checking for a store under Store API › Stores, or press Check now there.
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
