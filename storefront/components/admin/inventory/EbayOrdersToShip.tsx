"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { Badge, Notice, Pager, btn2Cls, inputCls } from "../ui";
import { LineStockButtons, OrderShipControls, type OrderLike, type Patch } from "../EbayOrderActions";

/* Inventory > Pick & ship > eBay orders: every eBay order whose parts are still set aside, oldest first.
   Per order: Open eBay label, tracking, Mark shipped (turns green). Per line: Mark picked / Release.
   A line leaves this list when it is picked (scanned out on the Ship screen or Mark picked), released, or its
   order is cancelled. The Orders page under Sales channels > eBay shows the same orders read-only. */

type Line = OrderLike & {
  store: string;
  ebay_item_id: string;
  custom_label: string | null;
  title: string | null;
  quantity: number;
  sku: string | null;
  product_id: number | null;
  sale_date: string | null;
};

const LIMIT = 50;

export default function EbayOrdersToShip({
  active,
  embedded = false,
  initialQ = "",
  reloadKey = 0,
  onEmpty,
}: {
  active: boolean;
  /** Shown for one order inside the combined queue: no intro, search or paging. */
  embedded?: boolean;
  initialQ?: string;
  /** Bump to re-read the list (after a scan-out was committed). */
  reloadKey?: number;
  /** Called when the list comes back empty, i.e. the order was picked, released or cancelled. */
  onEmpty?: () => void;
}) {
  const [lines, setLines] = useState<Line[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [q, setQ] = useState(initialQ);
  const [qInput, setQInput] = useState(initialQ);
  const emptyRef = useRef(onEmpty);
  emptyRef.current = onEmpty;
  const [msg, setMsg] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const p = new URLSearchParams({ offset: String(offset), limit: String(LIMIT) });
      if (q) p.set("q", q);
      const r = await api<{ total: number; items: Line[] }>(`/inventory/ebay-orders/lines?${p.toString()}`);
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

  /* tracking / shipped belong to the whole order: update every line of it on screen */
  const patchOrder = (orderPk: number, p: Patch) =>
    setLines((ls) => ls.map((l) => (l.order_pk === orderPk ? { ...l, ...p } : l)));

  return (
    <div className="space-y-3">
      {!embedded && (
      <p className="text-xs text-gray-500">
        eBay orders whose parts are still set aside, oldest first. <strong>Open eBay label</strong> to print the label on eBay, then{" "}
        <strong>Mark shipped</strong> with the tracking number (tracking from an eBay label fills in by itself). A line leaves this list when it
        is picked, released, or the order is cancelled.
      </p>
      )}
      {msg && <Notice kind="error">{msg}</Notice>}

      {!embedded && (
      <div className="flex flex-wrap items-end gap-2">
        <input
          className={`${inputCls} !w-[28rem] max-w-full`}
          placeholder="Order #, item #, tracking #, label or title"
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
        <button type="button" className={btn2Cls} disabled={loading} onClick={() => { setMsg(null); void load(); }}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>
      )}

      {!embedded && <Pager total={total} limit={LIMIT} offset={offset} onChange={setOffset} />}

      <div className="overflow-x-auto rounded border border-gray-200 bg-white">
        {/* fixed column widths, sized for the longest content, so nothing shifts from page to page */}
        <table className="w-full min-w-[80rem] table-fixed text-left text-sm">
          <colgroup>
            <col className="w-[11.5rem]" />
            <col className="w-[10rem]" />
            <col className="w-[10rem]" />
            <col className="w-[10rem]" />
            <col />
            <col className="w-[4rem]" />
            <col className="w-[17rem]" />
          </colgroup>
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr>
              <th className="whitespace-nowrap px-3 py-2">Date</th>
              <th className="whitespace-nowrap px-3 py-2">Store</th>
              <th className="whitespace-nowrap px-3 py-2">Order #</th>
              <th className="whitespace-nowrap px-3 py-2">Custom label</th>
              <th className="whitespace-nowrap px-3 py-2">Title</th>
              <th className="whitespace-nowrap px-3 py-2">Qty</th>
              <th className="whitespace-nowrap px-3 py-2">Fulfillment</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {lines.map((l) => (
              <tr key={l.id} className={l.shipped_at_ebay ? "bg-green-50" : ""}>
                <td className="whitespace-nowrap px-3 py-2 text-gray-600">{l.sale_date ? new Date(l.sale_date).toLocaleString() : "—"}</td>
                <td className="truncate px-3 py-2 text-gray-600" title={l.store}>{l.store}</td>
                <td className="whitespace-nowrap px-3 py-2">{l.ebay_order_id}</td>
                <td className="truncate px-3 py-2" title={l.custom_label ?? ""}>{l.custom_label || "—"}</td>
                <td className="px-3 py-2">
                  <div className="truncate" title={l.title ?? ""}>
                    {l.title ?? "(no title)"}
                  </div>
                  <div className="text-xs text-blue-600">
                    {l.ebay_item_id}
                    {l.sku ? ` · ${l.sku}` : ""}
                  </div>
                </td>
                <td className="px-3 py-2">{l.quantity}</td>
                <td className="px-3 py-2">
                  <div className="space-y-1.5">
                    <OrderShipControls o={l} onPatch={(p) => patchOrder(l.order_pk, p)} onError={setMsg} />
                    <LineStockButtons lineId={l.id} state={l.state} onDone={() => void load()} onError={setMsg} />
                    {l.shipped_at_ebay && <Badge tone="green">Shipped, waiting to be picked</Badge>}
                  </div>
                </td>
              </tr>
            ))}
            {lines.length === 0 && (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-gray-500">
                  {loading ? "Loading…" : "No eBay orders waiting. Everything set aside has been picked or released."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {!embedded && <Pager total={total} limit={LIMIT} offset={offset} onChange={setOffset} />}
    </div>
  );
}
